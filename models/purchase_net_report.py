# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import tools
from odoo import api, fields, models


class PurchaseNetReport(models.Model):
    _name = "purchase.net.report"
    _description = "Return Net Analysis Report"
    _auto = False
    _order = 'date_order desc'

    name = fields.Char('Order Reference', readonly=True)
    date_order = fields.Datetime('Order Date', readonly=True, help="Date on which this document has been created")
    trans_type = fields.Selection([
        ('Purchase Order', 'Purchase Order'),
        ('Return Order', 'Return Order')
    ], string='Transaction Type', readonly=True)
    product_id = fields.Many2one('product.product', 'Product', readonly=True)
    partner_id = fields.Many2one('res.partner', 'Vendor', readonly=True)
    product_uom = fields.Many2one('uom.uom', 'Reference Unit of Measure', required=True)
    company_id = fields.Many2one('res.company', 'Company', readonly=True)
    price_total = fields.Float('Total', readonly=True)
    price_average = fields.Float('Average Cost', readonly=True, group_operator="avg")
    category_id = fields.Many2one('product.category', 'Product Category', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', 'Product Template', readonly=True)
    country_id = fields.Many2one('res.country', 'Partner Country', readonly=True)
    account_analytic_id = fields.Many2one('account.analytic.account', 'Analytic Account', readonly=True)

    def _query(self, with_clause='', fields={}, groupby='', from_clause=''):
        with_ = ("WITH %s" % with_clause) if with_clause else ""

        select_ = """
                min(l.id) as id,
                po.name as name,
                po.date_order as date_order,
                'Purchase Order' as trans_type,  
                po.partner_id as partner_id,
                po.company_id as company_id, 
                l.product_id,
                p.product_tmpl_id,
                t.categ_id as category_id,
                t.uom_id as product_uom,
                sum(l.price_total)::decimal(16,2) as price_total,
                (sum(l.product_uom_qty * l.price_unit)/NULLIF(sum(l.product_uom_qty/line_uom.factor*product_uom.factor),0.0))::decimal(16,2) as price_average,
                partner.country_id as country_id
            """

        for field in fields.values():
            select_ += field

        from_ = """
                purchase_order_line l
                join purchase_order po on (l.order_id=po.id)
                join res_partner partner on po.partner_id = partner.id
                    left join product_product p on (l.product_id=p.id)
                        left join product_template t on (p.product_tmpl_id=t.id)
                left join uom_uom line_uom on (line_uom.id=l.product_uom)
                left join uom_uom product_uom on (product_uom.id=t.uom_id) %s
            """ % from_clause

        groupby_ = """
                po.name,
                po.company_id,
                po.partner_id, 
                l.price_unit,  
                l.product_uom,   
                l.product_id,
                p.product_tmpl_id,
                t.categ_id,
                trans_type,
                po.date_order,
                t.uom_id,
                line_uom.id,
                product_uom.factor,
                partner.country_id %s
            """ % groupby

        select2_ = """
                min(l.id) as id,
                po.name as name,
                po.date_order as date_order,
                'Return Order' as trans_type,  
                po.partner_id as partner_id,
                po.company_id as company_id, 
                l.product_id,
                p.product_tmpl_id,
                t.categ_id as category_id,
                t.uom_id as product_uom,
                sum(l.price_total)::decimal(16,2) *-1 as price_total,
                (sum(l.product_uom_qty * l.price_unit)/NULLIF(sum(l.product_uom_qty/line_uom.factor*product_uom.factor),0.0))::decimal(16,2) *-1 as price_average,
                partner.country_id as country_id 
                """

        for field in fields.values():
            select2_ += field

        from2_ = """
                purchase_return_line l
                join purchase_return po on (l.order_id=po.id)
                join res_partner partner on po.partner_id = partner.id
                    left join product_product p on (l.product_id=p.id)
                        left join product_template t on (p.product_tmpl_id=t.id)
                left join uom_uom line_uom on (line_uom.id=l.product_uom)
                left join uom_uom product_uom on (product_uom.id=t.uom_id) %s
            """ % from_clause

        groupby2_ = """
                po.name,
                po.company_id,
                po.partner_id, 
                l.price_unit,  
                l.product_uom,   
                l.product_id,
                p.product_tmpl_id,
                t.categ_id,
                trans_type,
                po.date_order,
                t.uom_id,
                line_uom.id,
                product_uom.factor,
                partner.country_id %s
                """ % groupby

        return '%s (SELECT %s FROM %s WHERE l.product_id IS NOT NULL GROUP BY %s Union SELECT %s FROM %s WHERE ' \
               'l.product_id IS NOT NULL GROUP BY %s)' % (with_, select_, from_, groupby_, select2_, from2_, groupby2_)

    def init(self):
        # self._table = sale_report
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""CREATE or REPLACE VIEW %s as (%s)""" % (self._table, self._query()))


class PurchaseNetReportProforma(models.AbstractModel):
    _name = 'purchase.net.report_saleproforma'
    _description = 'Proforma Report'

    def _get_report_values(self, docids, data=None):
        docs = self.env['purchase.net'].browse(docids)
        return {
            'doc_ids': docs.ids,
            'doc_model': 'purchase.net',
            'docs': docs,
            'proforma': True
        }
