# -*- coding: utf-8 -*-
# Copyright 2018 Doctor Any Time

{
    'name': 'SEPA Stripe',
    'description': 'SEPA Stripe',
    'version': '1.0.0',
    'license': 'LGPL-3',
    'author': '...',
    'website': '',
    'depends': [
        # 'payment',
        'payment_stripe', 'account_payment'
    ],
    'data': [
        'views/stripe_sepa_templates.xml',
        'data/payment_acquirer_data.xml'
    ],
    'installable': True,
}
