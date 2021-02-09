from odoo import fields, models, api


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    purchase_return_id = fields.Many2one(comodel_name='purchase.return')


class AccountMove(models.Model):
    _inherit = 'account.move'

    purchase_return_id = fields.Many2one(comodel_name='purchase.return')
