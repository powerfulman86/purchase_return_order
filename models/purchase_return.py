# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import datetime, timedelta
from functools import partial
from itertools import groupby
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import formatLang, get_lang
from odoo.osv import expression
from odoo.tools import float_is_zero, float_compare
from dateutil.relativedelta import relativedelta
from werkzeug.urls import url_encode


class PurchaseReturn(models.Model):
    _name = 'purchase.return'
    _inherit = ['portal.mixin', 'mail.thread', 'mail.activity.mixin', 'utm.mixin']
    _description = "Purchases Return"
    _order = 'id desc'

    @api.depends('order_line.price_total')
    def _amount_all(self):
        for order in self:
            amount_untaxed = amount_tax = 0.0
            for line in order.order_line:
                amount_untaxed += line.price_subtotal
                amount_tax += line.price_tax
            order.update({
                'amount_untaxed': order.amount_untaxed,
                'amount_tax': order.amount_tax,
                'amount_total': amount_untaxed + amount_tax,
            })

    name = fields.Char(string='Order Reference', required=True, copy=False, readonly=True,
                       states={'draft': [('readonly', False)]}, index=True, default=lambda self: _('New'))
    origin = fields.Char(string='Source Document',
                         help="Reference of the document that generated this Return order request.")
    client_order_ref = fields.Char(string='Vendor Reference', copy=False)
    reference = fields.Char(string='Payment Ref.', copy=False,
                            help='The payment communication of this purchase order.')
    READONLY_STATES = {
        'return': [('readonly', True)],
        'done': [('readonly', True)],
        'cancel': [('readonly', True)],
    }

    currency_id = fields.Many2one('res.currency', 'Currency', required=True, states=READONLY_STATES,
                                  default=lambda self: self.env.company.currency_id.id)
    state = fields.Selection([
        ('draft', 'Quotation'),
        ('return', 'Return Order'),
        ('done', 'Locked'),
        ('cancel', 'Cancelled'),
    ], string='Status', readonly=True, copy=False, index=True, tracking=3, default='draft')

    refund_done = fields.Boolean()
    picking_delivered = fields.Boolean(compute="_compute_picking_ids")

    user_id = fields.Many2one(
        'res.users', string='Salesperson', index=True, tracking=2, default=lambda self: self.env.user,
        domain=lambda self: [('groups_id', 'in', self.env.ref('sales_team.group_sale_salesman').id)])
    partner_id = fields.Many2one(
        'res.partner', string='Vendor', readonly=True,
        states={'draft': [('readonly', False)]},
        required=True, change_default=True, index=True, tracking=1,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]", )

    purchase_id = fields.Many2one("purchase.order", string="Purchase Order")

    order_line = fields.One2many('purchase.return.line', 'order_id', string='Order Lines',
                                 states={'cancel': [('readonly', True)], 'done': [('readonly', True)]}, copy=True,
                                 auto_join=True)
    company_id = fields.Many2one('res.company', string='Company', required=True,
                                 default=lambda self: self.env.user.company_id.id)

    def _get_invoiced(self):
        for rec in self:
            rec.invoice_count = len(rec.invoice_ids.ids)

    invoice_count = fields.Integer(string='Invoice Count', readonly=True)
    invoice_ids = fields.Many2many("account.move", string='Invoices', readonly=True,
                                   copy=False)

    note = fields.Text('Terms and conditions')

    amount_untaxed = fields.Float(string='Untaxed Amount', store=True, readonly=True, compute='_amount_all',
                                  tracking=5)
    amount_tax = fields.Float(string='Taxes', store=True, readonly=True, compute='_amount_all')
    amount_total = fields.Float(string='Total', store=True, readonly=True, compute='_amount_all', tracking=4)

    def _default_company_id(self):
        if self.env.user.company_id:
            return self.env.user.company_id.id
        return self.env['res.company'].search([], limit=1)

    # company_id = fields.Many2one('res.company', string='Company', required=True, default=_default_company_id)
    date_order = fields.Datetime(string='Order Date', readonly=True, copy=False, states={'draft': [('readonly', False)]},
                             default=fields.Date.context_today)

    @api.onchange('purchase_id')
    def change_purchase_id(self):
        if self.purchase_id:
            lines = []
            self.partner_id = self.purchase_id.partner_id.id
            self.warehouse_id = self.purchase_id.picking_type_id.warehouse_id.id
            self.user_id = self.purchase_id.user_id.id
            self.company_id = self.purchase_id.company_id.id
            self.date_order = self.purchase_id.date_order
            for line in self.purchase_id.order_line:
                values = {
                    'product_id': line.product_id.id,
                    'name': line.name,
                    'product_uom_qty': line.product_qty,
                    'product_uom': line.product_uom.id,
                    'price_unit': line.price_unit,
                    'taxes_id': [(6, 0, line.taxe_id.ids)],
                }
                lines.append((0, 0, values))
            print(lines)
            self.order_line = None
            self.order_line = lines

    @api.onchange('partner_id', 'company_id')
    def onchange_partner_id(self):
        # Ensures all properties and fiscal positions
        # are taken with the company of the order
        # if not defined, force_company doesn't change anything.
        self = self.with_context(force_company=self.company_id.id)
        if not self.partner_id:
            # self.fiscal_position_id = False
            self.currency_id = self.env.company.currency_id.id
        else:
            # self.fiscal_position_id = self.env['account.fiscal.position'].get_fiscal_position(self.partner_id.id)
            # self.payment_term_id = self.partner_id.property_supplier_payment_term_id.id
            self.currency_id = self.partner_id.property_purchase_currency_id.id or self.env.company.currency_id.id
        return {}

    def create_refund(self):
        self.ensure_one()
        journal = self.env['account.journal'].search([('type', '=', 'purchase')], limit=1)

        if not journal:
            raise UserError(_('Please define an accounting purchases journal for the company %s (%s).') % (
                self.company_id.name, self.company_id.id))

        lines = []
        for order_line in self.order_line:
            vals = {
                'product_id': order_line.product_id.id,
                'quantity': order_line.product_qty if self.amount_total >= 0 else -order_line.product_qty,
                'discount': order_line.discount,
                'product_uom_id': order_line.product_uom.id,
                'price_unit': order_line.price_unit,
                'name': order_line.product_id.display_name,
                'tax_ids': [(6, 0, order_line.taxes_id.ids)],
            }

            lines.append((0, 0, vals))

        invoice_vals = {
            'ref': self.client_order_ref or '',
            'type': 'in_refund',
            'purchase_return_id': self.id,
            'narration': self.note,
            'invoice_user_id': self.user_id and self.user_id.id,
            'partner_id': self.partner_id.id,
            'invoice_origin': self.name,
            'invoice_payment_ref': self.reference,
            'invoice_line_ids': lines,
        }
        invoice_id = self.env['account.move'].create(invoice_vals)
        self.invoice_ids = [(6, 0, [invoice_id.id])]
        view_id = self.env.ref('account.view_move_form').id
        self.refund_done = True
        return {
            'name': _('View Credit Note'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'target': 'current',
            'res_model': 'account.move',
            'view_id': view_id,
            'res_id': invoice_id.id
        }
        return result

    @api.model
    def _default_picking_type_id(self):
        pick_ids = self.env['stock.picking.type'].search([('code', '=', 'incoming')], limit=1)
        return pick_ids

    picking_type_id = fields.Many2one(
        'stock.picking.type', string='Deliver To',
        required=True, readonly=True, states={'draft': [('readonly', False)]},
        default=_default_picking_type_id)

    @api.model
    def _default_warehouse_id(self):
        company = self.env.user.company_id.id
        warehouse_ids = self.env['stock.warehouse'].search([], limit=1)
        return warehouse_ids

    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse',
        required=True, readonly=True, states={'draft': [('readonly', False)]},
        default=_default_warehouse_id)

    picking_ids = fields.One2many('stock.picking', 'purchase_return_id', string='Transfers')
    move_ids = fields.One2many('account.move', 'purchase_return_id', string='Credit')
    date_planned = fields.Datetime(string='Delivery Date', index=True)
    #
    # _sql_constraints = [
    #     ('date_order_conditional_required',
    #      "CHECK( (state IN ('sale', 'done') AND date_order IS NOT NULL) OR state NOT IN ('sale', 'done') )",
    #      "A confirmed sales order requires a confirmation date."),
    # ]

    def unlink(self):
        for order in self:
            if order.state not in ('draft', 'cancel'):
                raise UserError(
                    _('You can not delete a sent quotation or a confirmed return order. You must first cancel it.'))
        return super(PurchaseReturn, self).unlink()

    @api.model
    def create(self, vals):
        if vals.get('name', _('New')) == _('New'):
            seq_date = None
            if 'date_order' in vals:
                seq_date = fields.Datetime.context_timestamp(self, fields.Datetime.to_datetime(vals['date_order']))
            if 'company_id' in vals:
                vals['name'] = self.env['ir.sequence'].with_context(force_company=vals['company_id']).next_by_code(
                    'purchase.return', sequence_date=seq_date) or _('New')
            else:
                vals['name'] = self.env['ir.sequence'].next_by_code('purchase.return', sequence_date=seq_date) or _(
                    'New')

        result = super(PurchaseReturn, self).create(vals)
        return result

    def name_get(self):
        if self._context.get('sale_show_partner_name'):
            res = []
            for order in self:
                name = order.name
                if order.partner_id.name:
                    name = '%s - %s' % (name, order.partner_id.name)
                res.append((order.id, name))
            return res
        return super(PurchaseReturn, self).name_get()

    @api.model
    def _name_search(self, name, args=None, operator='ilike', limit=100, name_get_uid=None):

        if operator == 'ilike' and not (name or '').strip():
            domain = []
        elif operator in ('ilike', 'like', '=', '=like', '=ilike'):
            domain = expression.AND([
                args or [],
                ['|', ('name', operator, name), ('partner_id.name', operator, name)]
            ])
            order_ids = self._search(domain, limit=limit, access_rights_uid=name_get_uid)
            return models.lazy_name_get(self.browse(order_ids).with_user(name_get_uid))
        return super(PurchaseReturn, self)._name_search(name, args=args, operator=operator, limit=limit,
                                                        name_get_uid=name_get_uid)

    def action_view_invoice(self):
        invoices = self.mapped('invoice_ids')
        action = self.env.ref('account.action_move_out_invoice_type').read()[0]
        if len(invoices) > 1:
            action['domain'] = [('id', 'in', invoices.ids)]
        elif len(invoices) == 1:
            form_view = [(self.env.ref('account.view_move_form').id, 'form')]
            if 'views' in action:
                action['views'] = form_view + [(state, view) for state, view in action['views'] if view != 'form']
            else:
                action['views'] = form_view
            action['res_id'] = invoices.id
        else:
            action = {'type': 'ir.actions.act_window_close'}

        context = {
            'default_type': 'out_invoice',
        }
        if len(self) == 1:
            context.update({
                'default_partner_id': self.partner_id.id,
                'default_invoice_origin': self.mapped('name'),
                'default_user_id': self.user_id.id,
            })
        action['context'] = context
        return action

    def action_draft(self):
        orders = self.filtered(lambda s: s.state in ['cancel'])
        return orders.write({
            'state': 'draft',
        })

    def action_cancel(self):
        return self.write({'state': 'cancel'})

    def action_done(self):
        # for order in self:
        #     tx = order.sudo().transaction_ids.get_last_transaction()
        #     if tx and tx.state == 'pending' and tx.acquirer_id.provider == 'transfer':
        #         tx._set_transaction_done()
        #         tx.write({'is_processed': True})
        return self.write({'state': 'done'})

    def action_unlock(self):
        self.write({'state': 'return'})

    def _action_confirm(self):
        """ Implementation of additionnal mecanism of Sales Order confirmation.
            This method should be extended when the confirmation should generated
            other documents. In this method, the SO are in 'sale' state (not yet 'done').
        """
        # create an analytic account if at least an expense product
        self._create_stock()
        if self.purchase_id:
            for line in self.order_line:
                for p_line in self.purchase_id.order_line:
                    if line.product_id == p_line.product_id and line.product_uom_qty > p_line.product_qty:
                        raise ValidationError(_(
                            "Can not return Quantity more than [ %s ] Purchased for product [ %s ]" % (
                                p_line.product_uom_qty, line.product_id.name)))
        else:
            for line in self.order_line:
                if line.product_id.qty_available < line.product_uom_qty:
                    raise ValidationError(_("Can not return Quantity more than [ %s ] Available for product [ %s ]" % (
                        line.product_uom_qty, line.product_id.name)))

        return True

    def action_confirm(self):
        self.write({
            'state': 'return',
            'date_order': fields.Datetime.now()})
        self._action_confirm()
        return True

    def _get_forbidden_state_confirm(self):
        return {'done', 'cancel'}

    def _create_stock(self):
        pickings = self.mapped('picking_ids')
        picking_id = self.env["stock.picking"].create({
            'partner_id': self.partner_id.id,
            'origin': self.name,
            'scheduled_date': self.date_planned,
            'picking_type_id': self.env['stock.picking.type'].search([('code', '=', 'outgoing')])[0].id,
            'location_id': self.warehouse_id.lot_stock_id.id,
            'location_dest_id': self.partner_id.property_stock_supplier.id,

        })
        for line in self.order_line:
            self.env['stock.move'].create({
                'picking_id': picking_id.id,
                'product_id': line.product_id.id,
                'name': "Purchase Return " + self.name,
                'product_uom_qty': line.product_uom_qty,
                'location_id': self.warehouse_id.lot_stock_id.id,
                'location_dest_id': self.partner_id.property_stock_supplier.id,
                'product_uom': line.product_id.uom_id.id,
            })
            picking_id.action_confirm()
            picking_id.action_assign()
        self.picking_ids = [(6, 0, [picking_id.id])]

    def action_view_receipt(self):
        '''
        This function returns an action that display existing receipt orders
        of given sales order ids. It can either be a in a list or in a form
        view, if there is only one receipt order to show.
        '''
        action = self.env.ref('stock.action_picking_tree_all').read()[0]

        pickings = self.mapped('picking_ids')
        if len(pickings) > 1:
            action['domain'] = [('id', 'in', pickings.ids)]
        elif pickings:
            form_view = [(self.env.ref('stock.view_picking_form').id, 'form')]
            if 'views' in action:
                action['views'] = form_view + [(state, view) for state, view in action['views'] if view != 'form']
            else:
                action['views'] = form_view
            action['res_id'] = pickings.id
        # Prepare the context.
        picking_id = pickings.filtered(lambda l: l.picking_type_id.code == 'outgoing')
        if picking_id:
            picking_id = picking_id[0]
        else:
            picking_id = pickings[0]
        action['context'] = dict(self._context, default_partner_id=self.partner_id.id, default_picking_id=picking_id.id,
                                 default_picking_type_id=picking_id.picking_type_id.id, default_origin=self.name,
                                 default_group_id=picking_id.group_id.id)
        return action

    def action_view_purchase_return(self):
        view_id = self.env.ref('account.view_move_form').id
        return {
            'name': _('View Credit Note'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'target': 'current',
            'res_model': 'account.move',
            'view_id': view_id,
            'res_id': self.invoice_ids[0].id
        }

    receipts_count = fields.Integer(string='Receipt Orders', compute='_compute_picking_ids')
    refund_count = fields.Integer(string='Credit notes', compute='_compute_refund')

    @api.depends('picking_ids')
    def _compute_picking_ids(self):
        for rec in self:
            rec.receipts_count = len(rec.picking_ids)
            if rec.picking_ids:
                for pick in rec.picking_ids:
                    if pick.state == 'done':
                        rec.picking_delivered = True
                    else:
                        rec.picking_delivered = False
            else:
                rec.picking_delivered = False

    @api.depends('move_ids')
    def _compute_refund(self):
        for rec in self:
            rec.refund_count = len(rec.move_ids.ids)


class PurchaseReturnLine(models.Model):
    _name = 'purchase.return.line'
    _description = 'Return Order Line'
    _order = 'order_id, sequence, id'
    _check_company_auto = True

    order_id = fields.Many2one('purchase.return', string='Order Reference', required=False, ondelete='cascade',
                               index=True,
                               copy=False)
    name = fields.Text(string='Description', required=True)
    sequence = fields.Integer(string='Sequence', default=10)
    invoice_lines = fields.Many2many('account.move.line', 'purchase_return_line_invoice_rel', 'order_line_id',
                                     'invoice_line_id', string='Invoice Lines', copy=False)
    price_unit = fields.Float('Unit Price', required=False, digits='Product Price', default=0.0)
    price_subtotal = fields.Float(string='Subtotal', compute="_compute_amount", readonly=True, store=True)
    price_tax = fields.Float(string='Total Tax', compute="_compute_amount", readonly=True, store=True)
    price_total = fields.Float(string='Total', compute="_compute_amount", readonly=True, store=True)
    taxes_id = fields.Many2many('account.tax', string='Taxes',
                                domain=['|', ('active', '=', False), ('active', '=', True)])
    discount = fields.Float(string='Discount (%)', digits='Discount', default=0.0)
    product_id = fields.Many2one(
        'product.product', string='Product', required=1,
        domain="[('purchase_ok', '=', True), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        change_default=True, ondelete='restrict', check_company=True)  # Unrequired company
    product_template_id = fields.Many2one(
        'product.template', string='Product Template',
        related="product_id.product_tmpl_id", domain=[('purchase_ok', '=', True)])
    product_updatable = fields.Boolean(string='Can Edit Product', readonly=True,
                                       default=True)
    product_qty = fields.Float(string='Quantity', digits='Product Unit of Measure', required=True)
    product_uom_qty = fields.Float(string='Total Quantity', compute='_compute_product_uom_qty', store=True)
    product_uom = fields.Many2one('uom.uom', string='Unit of Measure',
                                  domain="[('category_id', '=', product_uom_category_id)]")
    product_uom_category_id = fields.Many2one(related='product_id.uom_id.category_id', readonly=True)
    order_partner_id = fields.Many2one(related='order_id.partner_id', store=True, string='Vendor', readonly=False)
    account_analytic_id = fields.Many2one('account.analytic.account', string='Analytic Account')
    analytic_tag_ids = fields.Many2many('account.analytic.tag', string='Analytic Tags')
    date_planned = fields.Datetime(string='Scheduled Date', index=True)
    partner_id = fields.Many2one('res.partner', related='order_id.partner_id', string='Partner', readonly=True,
                                 store=True)
    date_order = fields.Datetime(related='order_id.date_order', string='Order Date', readonly=True)

    def _default_company_id(self):
        if self.env.user.company_id:
            return self.env.user.company_id.id
        return self.env['res.company'].search([], limit=1)

    company_id = fields.Many2one('res.company', string='Company', required=True, default=_default_company_id)

    @api.onchange('product_id')
    def onchange_product_id(self):
        if not self.product_id:
            return

        # Reset date, price and quantity since _onchange_quantity will provide default values
        self.date_planned = datetime.today().strftime(DEFAULT_SERVER_DATETIME_FORMAT)
        self.price_unit = self.product_qty = 0.0
        self._product_id_change()
        self._suggest_quantity()
        self._onchange_quantity()

    def _product_id_change(self):
        if not self.product_id:
            return

        self.product_uom = self.product_id.uom_po_id or self.product_id.uom_id
        product_lang = self.product_id.with_context(
            lang=get_lang(self.env, self.partner_id.lang).code,
            partner_id=self.partner_id.id,
            company_id=self.company_id.id,
        )
        self.name = self._get_product_purchase_description(product_lang)

        # self._compute_tax_id()

    @api.depends('product_qty', 'discount', 'price_unit', 'taxes_id')
    def _compute_amount(self):
        """
        Compute the amounts of the SO line.
        """
        for line in self:
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            taxes = line.taxes_id.compute_all(price, False, line.product_qty, product=line.product_id,
                                            partner=line.order_id.partner_id)
            line.update({
                'price_tax': sum(t.get('amount', 0.0) for t in taxes.get('taxes', [])),
                'price_total': taxes['total_included'],
                'price_subtotal': taxes['total_excluded'],
            })

    @api.depends('product_uom', 'product_qty', 'product_id.uom_id')
    def _compute_product_uom_qty(self):
        for line in self:
            if line.product_id and line.product_id.uom_id != line.product_uom:
                line.product_uom_qty = line.product_uom._compute_quantity(line.product_qty, line.product_id.uom_id)
            else:
                line.product_uom_qty = line.product_qty

    def _suggest_quantity(self):
        '''
        Suggest a minimal quantity based on the seller
        '''
        if not self.product_id:
            return
        seller_min_qty = self.product_id.seller_ids\
            .filtered(lambda r: r.name == self.order_id.partner_id and (not r.product_id or r.product_id == self.product_id))\
            .sorted(key=lambda r: r.min_qty)
        if seller_min_qty:
            self.product_qty = seller_min_qty[0].min_qty or 1.0
            self.product_uom = seller_min_qty[0].product_uom
        else:
            self.product_qty = 1.0

    def _get_product_purchase_description(self, product_lang):
        self.ensure_one()
        name = product_lang.display_name
        if product_lang.description_purchase:
            name += '\n' + product_lang.description_purchase

        return name

    @api.model
    def _get_date_planned(self, seller, po=False):
        """Return the datetime value to use as Schedule Date (``date_planned``) for
           PO Lines that correspond to the given product.seller_ids,
           when ordered at `date_order_str`.

           :param Model seller: used to fetch the delivery delay (if no seller
                                is provided, the delay is 0)
           :param Model po: purchase.order, necessary only if the PO line is
                            not yet attached to a PO.
           :rtype: datetime
           :return: desired Schedule Date for the PO line
        """
        date_order = po.date_order if po else self.order_id.date_order
        if date_order:
            return date_order + relativedelta(days=seller.delay if seller else 0)
        else:
            return datetime.today() + relativedelta(days=seller.delay if seller else 0)

    @api.onchange('product_qty', 'product_uom')
    def _onchange_quantity(self):
        if not self.product_id:
            return
        params = {'order_id': self.order_id}
        seller = self.product_id._select_seller(
            partner_id=self.partner_id,
            quantity=self.product_qty,
            date=self.order_id.date_order and self.order_id.date_order.date(),
            uom_id=self.product_uom,
            params=params)

        if seller or not self.date_planned:
            self.date_planned = self._get_date_planned(seller).strftime(DEFAULT_SERVER_DATETIME_FORMAT)

        if not seller:
            if self.product_id.seller_ids.filtered(lambda s: s.name.id == self.partner_id.id):
                self.price_unit = 0.0
            return

        price_unit = self.env['account.tax']._fix_tax_included_price_company(seller.price,
                                                                             self.product_id.supplier_taxes_id,
                                                                             self.taxes_id,
                                                                             self.company_id) if seller else 0.0
        if price_unit and seller and self.order_id.currency_id and seller.currency_id != self.order_id.currency_id:
            price_unit = seller.currency_id._convert(
                price_unit, self.order_id.currency_id, self.order_id.company_id, self.date_order or fields.Date.today())

        if seller and self.product_uom and seller.product_uom != self.product_uom:
            price_unit = seller.product_uom._compute_price(price_unit, self.product_uom)

        self.price_unit = price_unit
