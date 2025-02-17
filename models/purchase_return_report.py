# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import tools
from odoo import api, fields, models


class PurchaseReturnReport(models.Model):
    _name = "purchase.return.report"
    _description = "Return Analysis Report"
    _auto = False
    _order = 'date_order desc'

    date_order = fields.Datetime('Order Date', readonly=True, help="Date on which this document has been created")
    state = fields.Selection([
        ('draft', 'Draft RFQ'),
        ('sent', 'RFQ Sent'),
        ('to approve', 'To Approve'),
        ('purchase', 'Purchase Order'),
        ('done', 'Done'),
        ('cancel', 'Cancelled')
    ], 'Order Status', readonly=True)
    product_id = fields.Many2one('product.product', 'Product', readonly=True)
    partner_id = fields.Many2one('res.partner', 'Vendor', readonly=True)
    product_uom = fields.Many2one('uom.uom', 'Reference Unit of Measure', required=True)
    company_id = fields.Many2one('res.company', 'Company', readonly=True)
    user_id = fields.Many2one('res.users', 'Purchase Representative', readonly=True)
    delay = fields.Float('Days to Confirm', digits=(16, 2), readonly=True)
    delay_pass = fields.Float('Days to Receive', digits=(16, 2), readonly=True)
    price_total = fields.Float('Total', readonly=True)
    price_average = fields.Float('Average Cost', readonly=True, group_operator="avg")
    category_id = fields.Many2one('product.category', 'Product Category', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', 'Product Template', readonly=True)
    country_id = fields.Many2one('res.country', 'Partner Country', readonly=True)
    account_analytic_id = fields.Many2one('account.analytic.account', 'Analytic Account', readonly=True)
    commercial_partner_id = fields.Many2one('res.partner', 'Commercial Entity', readonly=True)
    weight = fields.Float('Gross Weight', readonly=True)
    volume = fields.Float('Volume', readonly=True)
    order_id = fields.Many2one('purchase.order', 'Order', readonly=True)
    qty_ordered = fields.Float('Qty Ordered', readonly=True)

    def init(self):
        # self._table = sale_report
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""CREATE or REPLACE VIEW %s as (
                %s
                FROM ( %s )
                %s
                )""" % (self._table, self._select(), self._from(), self._group_by()))

    def _select(self):
        select_str = """
                WITH currency_rate as (%s)
                    SELECT
                        po.id as order_id,
                        min(l.id) as id,
                        po.date_order as date_order,
                        po.state,  
                        po.partner_id as partner_id,
                        po.user_id as user_id,
                        po.company_id as company_id, 
                        l.product_id,
                        p.product_tmpl_id,
                        t.categ_id as category_id, 
                        sum(l.product_qty / line_uom.factor * product_uom.factor) as qty_ordered,
                        t.uom_id as product_uom,
                        sum(l.price_total)::decimal(16,2) as price_total,
                        (sum(l.product_uom_qty * l.price_unit)/NULLIF(sum(l.product_uom_qty/line_uom.factor*product_uom.factor),0.0))::decimal(16,2) as price_average,
                        partner.country_id as country_id,
                        partner.commercial_partner_id as commercial_partner_id,
                        sum(p.weight * l.product_uom_qty/line_uom.factor*product_uom.factor) as weight,
                        sum(p.volume * l.product_uom_qty/line_uom.factor*product_uom.factor) as volume 
            """ % self.env['res.currency']._select_companies_rates()
        return select_str

    def _from(self):
        from_str = """
                purchase_return_line l
                    join purchase_return po on (l.order_id=po.id)
                    join res_partner partner on po.partner_id = partner.id
                        left join product_product p on (l.product_id=p.id)
                            left join product_template t on (p.product_tmpl_id=t.id)
                    left join uom_uom line_uom on (line_uom.id=l.product_uom)
                    left join uom_uom product_uom on (product_uom.id=t.uom_id)
                        
            """
        return from_str

    def _group_by(self):
        group_by_str = """
                GROUP BY
                    po.company_id,
                    po.user_id,
                    po.partner_id,
                    line_uom.factor, 
                    l.price_unit,  
                    l.product_uom,   
                    l.product_id,
                    p.product_tmpl_id,
                    t.categ_id,
                    po.date_order,
                    po.state,
                    line_uom.uom_type,
                    line_uom.category_id,
                    t.uom_id,
                    t.purchase_method,
                    line_uom.id,
                    product_uom.factor,
                    partner.country_id,
                    partner.commercial_partner_id, 
                    po.id
            """
        return group_by_str
