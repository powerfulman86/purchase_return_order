# -*- coding: utf-8 -*-
{
    'name': "purchase_return_order",
    'summary': """Purchase Return order""",
    'description': """Purchase Return Order""",
    'author': "",
    'category': 'Uncategorized',
    'version': '13.0.0.1',
    'depends': ['base', 'sale', 'purchase', 'stock'],

    'data': [
        'security/ir.model.access.csv',
        'data/data.xml',
        'views/purchase_return.xml',
        'views/purchase_return_report.xml',
    ],

}
