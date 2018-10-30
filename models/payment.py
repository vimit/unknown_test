# coding: utf-8

import logging
import json
import pprint
import stripe
from odoo.tools import float_compare

from odoo import api, fields, models, _
from odoo.addons.payment.models.payment_acquirer import ValidationError
from odoo.tools.float_utils import float_round

_logger = logging.getLogger(__name__)

# Force the API version to avoid breaking in case of update on Stripe side
# cf https://stripe.com/docs/api#versioning
# changelog https://stripe.com/docs/upgrades#api-changelog
STRIPE_HEADERS = {'Stripe-Version': '2016-03-07'}

# The following currencies are integer only, see https://stripe.com/docs/currencies#zero-decimal
INT_CURRENCIES = [
    u'BIF', u'XAF', u'XPF', u'CLP', u'KMF', u'DJF', u'GNF', u'JPY', u'MGA', u'PYG', u'RWF', u'KRW',
    u'VUV', u'VND', u'XOF'
]


class PaymentAcquirerStripe(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[('stripe', 'Stripe')])
    stripe_secret_key = fields.Char(required_if_provider='stripe', groups='base.group_user')
    stripe_publishable_key = fields.Char(required_if_provider='stripe', groups='base.group_user')
    stripe_image_url = fields.Char(
        "Checkout Image URL", groups='base.group_user',
        help="A relative or absolute URL pointing to a square image of your "
             "brand or product. As defined in your Stripe profile. See: "
             "https://stripe.com/docs/checkout")

    @api.multi
    def stripe_form_generate_values(self, tx_values):
        self.ensure_one()
        stripe_tx_values = dict(tx_values)
        temp_stripe_tx_values = {
            'company': self.company_id.name,
            'amount': tx_values['amount'],  # Mandatory
            'currency': tx_values['currency'].name,  # Mandatory anyway
            'currency_id': tx_values['currency'].id,  # same here
            'address_line1': tx_values.get('partner_address'),  # Any info of the partner is not mandatory
            'address_city': tx_values.get('partner_city'),
            'address_country': tx_values.get('partner_country') and tx_values.get('partner_country').name or '',
            'email': tx_values.get('partner_email'),
            'address_zip': tx_values.get('partner_zip'),
            'name': tx_values.get('partner_name'),
            'phone': tx_values.get('partner_phone'),
        }

        temp_stripe_tx_values['returndata'] = stripe_tx_values.pop('return_url', '')
        stripe_tx_values.update(temp_stripe_tx_values)
        return stripe_tx_values

    @api.model
    def _get_stripe_api_url(self):
        return 'api.stripe.com/v1'

    @api.model
    def stripe_s2s_form_process(self, data):
        payment_token = self.env['payment.token'].sudo().create({
            'iban': data['iban'],
            'acquirer_id': int(data['acquirer_id']),
            'partner_id': int(data['partner_id'])
        })
        return payment_token

    @api.multi
    def stripe_s2s_form_validate(self, data):
        self.ensure_one()
        # mandatory fields
        for field_name in ["iban", ]:
            if not data.get(field_name):
                return False
        return True

    def _get_feature_support(self):
        """Get advanced feature support by provider.

        Each provider should add its technical in the corresponding
        key for the following features:
            * fees: support payment fees computations
            * authorize: support authorizing payment (separates
                         authorization and capture)
            * tokenize: support saving payment data in a payment.tokenize
                        object
        """

        res = super(PaymentAcquirerStripe, self)._get_feature_support()
        res['tokenize'].append('stripe')
        return res


class PaymentTransactionStripe(models.Model):
    _inherit = 'payment.transaction'

    def _create_stripe_charge(self, acquirer_ref=None, tokenid=None, email=None):

        payment_acquirer = self.env['payment.acquirer'].browse(self.acquirer_id.id)
        stripe.api_key = payment_acquirer.stripe_publishable_key
        charge = stripe.Charge.create(
            amount=int(self.amount if self.currency_id.name in INT_CURRENCIES else float_round(self.amount * 100, 2)),
            currency='eur',
            customer=acquirer_ref,
            source=str(tokenid),
        )
        return charge

    @api.multi
    def stripe_s2s_do_transaction(self, **kwargs):
        self.ensure_one()
        result = self._create_stripe_charge(acquirer_ref=self.payment_token_id.acquirer_ref, tokenid=self.payment_token_id.name, email=self.partner_email)
        return self._stripe_s2s_validate_tree(result)

    # def _create_stripe_refund(self):
    #     api_url_refund = 'https://%s/refunds' % (self.acquirer_id._get_stripe_api_url())
    #
    #     refund_params = {
    #         'charge': self.acquirer_reference,
    #         'amount': int(float_round(self.amount * 100, 2)), # by default, stripe refund the full amount (we don't really need to specify the value)
    #         'metadata[reference]': self.reference,
    #     }
    #
    #     r = requests.post(api_url_refund,
    #                         auth=(self.acquirer_id.stripe_secret_key, ''),
    #                         params=refund_params,
    #                         headers=STRIPE_HEADERS)
    #     return r.json()

    # @api.multi
    # def stripe_s2s_do_refund(self, **kwargs):
    #     self.ensure_one()
    #     self.state = 'refunding'
    #     result = self._create_stripe_refund()
    #     return self._stripe_s2s_validate_tree(result)

    @api.model
    def _stripe_form_get_tx_from_data(self, data):
        """ Given a data dict coming from stripe, verify it and find the related
        transaction record. """
        reference = data.get('metadata', {}).get('reference')
        if not reference:
            stripe_error = data.get('error', {}).get('message', '')
            _logger.error('Stripe: invalid reply received from stripe API, looks like '
                          'the transaction failed. (error: %s)', stripe_error  or 'n/a')
            error_msg = _("We're sorry to report that the transaction has failed.")
            if stripe_error:
                error_msg += " " + (_("Stripe gave us the following info about the problem: '%s'") %
                                    stripe_error)
            error_msg += " " + _("Perhaps the problem can be solved by double-checking your "
                                 "credit card details, or contacting your bank?")
            raise ValidationError(error_msg)

        tx = self.search([('reference', '=', reference)])
        if not tx:
            error_msg = (_('Stripe: no order found for reference %s') % reference)
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        elif len(tx) > 1:
            error_msg = (_('Stripe: %s orders found for reference %s') % (len(tx), reference))
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        return tx[0]

    @api.multi
    def _stripe_s2s_validate_tree(self, tree):
        self.ensure_one()
        if self.state not in ('draft', 'pending', 'refunding'):
            _logger.info('Stripe: trying to validate an already validated tx (ref %s)', self.reference)
            return True

        status = tree.get('status')
        if status == 'pending':
            new_state = 'pending'
            self.write({
                'state': new_state,
                'date_validate': fields.datetime.now(),
                'acquirer_reference': tree.get('id'),
            })
            self.execute_callback()
            if self.payment_token_id:
                self.payment_token_id.verified = True
            _logger.warning('Waiting For Confirmation')
            return True
        else:
            error = tree['error']['message']
            _logger.warn(error)
            self.sudo().write({
                'state': 'error',
                'state_message': error,
                'acquirer_reference': tree.get('id'),
                'date_validate': fields.datetime.now(),
            })
            return False

    @api.multi
    def _stripe_form_get_invalid_parameters(self, data):
        invalid_parameters = []
        reference = data['metadata']['reference']
        if reference != self.reference:
            invalid_parameters.append(('Reference', reference, self.reference))
        return invalid_parameters

    @api.multi
    def _stripe_form_validate(self,  data):
        return self._stripe_s2s_validate_tree(data)

    def confirm_invoice_token(self):
        """ Confirm a transaction token and call SO confirmation if it is a success.

        :return: True if success; error string otherwise """
        self.ensure_one()
        if self.payment_token_id and self.partner_id == self.account_invoice_id.partner_id:
            try:
                s2s_result = self.stripe_s2s_do_transaction()
            except Exception as e:
                _logger.warning(
                    _("<%s> transaction (%s) failed : <%s>") %
                    (self.acquirer_id.provider, self.id, str(e)))
                return 'pay_invoice_tx_fail'

            valid_state = 'pending'
            if not s2s_result or self.state != valid_state:
                print('problem is here 1')
                _logger.warning(
                    _("<%s> transaction (%s) invalid state : %s") %
                    (self.acquirer_id.provider, self.id, self.state_message))
                return 'pay_invoice_tx_state'

            try:
                # Auto-confirm SO if necessary
                return self._confirm_invoice()
            except Exception as e:
                print('problem is here 2')
                _logger.warning(
                    _("<%s>  transaction (%s) invoice confirmation failed : <%s>") %
                    (self.acquirer_id.provider, self.id, str(e)))
                return 'pay_invoice_tx_confirm'
        return 'pay_invoice_tx_token'

    def _confirm_invoice(self):
        """ Check tx state, confirm and pay potential invoice """
        self.ensure_one()
        # check tx state, confirm the potential SO
        if self.account_invoice_id.state != 'open':
            _logger.warning('<%s> transaction STATE INCORRECT for invoice %s (ID %s, state %s)', self.acquirer_id.provider, self.account_invoice_id.number, self.account_invoice_id.id, self.account_invoice_id.state)
            return 'pay_invoice_invalid_doc_state'
        if not float_compare(self.amount, self.account_invoice_id.amount_total, 2) == 0:
            _logger.warning(
                '<%s> transaction AMOUNT MISMATCH for invoice %s (ID %s): expected %r, got %r',
                self.acquirer_id.provider, self.account_invoice_id.number, self.account_invoice_id.id,
                self.account_invoice_id.amount_total, self.amount,
            )
            self.account_invoice_id.message_post(
                subject=_("Amount Mismatch (%s)") % self.acquirer_id.provider,
                body=_("The invoice was not confirmed despite response from the acquirer (%s): invoice amount is %r but acquirer replied with %r.") % (
                    self.acquirer_id.provider,
                    self.account_invoice_id.amount_total,
                    self.amount,
                )
            )
            return 'pay_invoice_tx_amount'

        if self.state == 'authorized' and self.acquirer_id.capture_manually:
            _logger.info('<%s> transaction authorized, nothing to do with invoice %s (ID %s)', self.acquirer_id.provider, self.account_invoice_id.number, self.account_invoice_id.id)
        elif self.state == 'pending':
            _logger.info('<%s> transaction pending, paying invoice %s (ID %s) in few days,', self.acquirer_id.provider, self.account_invoice_id.number, self.account_invoice_id.id)
        else:
            _logger.warning('<%s> transaction MISMATCH for invoice %s (ID %s)', self.acquirer_id.provider, self.account_invoice_id.number, self.account_invoice_id.id)
            return 'pay_invoice_tx_state'
        return True

    @api.model
    def transaction_status(self):
        self.transaction_status_event_listener()

    def transaction_status_event_listener(self):
        payment_acquirer = self.env['payment.acquirer'].browse(self.acquirer_id.id)
        stripe.api_key = payment_acquirer.stripe_publishable_key
        events = stripe.Event.list()
        for event in events:
            if 'charge' in event.get('type'):
                charge = event.get('data')['object']
                if charge.get('id') == self.acquirer_reference:
                    if event.get('type') == 'charge.succeeded':
                        self._pay_invoice()
                        print('success')
                    if event.get('type') == 'charge.failed':
                        # send mail to user
                        print('failed')
                    if event.get('type') == 'charge.expired':
                        # send mail to user
                        print('expired')
                    if event.get('type') == 'charge.pending':
                        print('pending')


class PaymentTokenStripe(models.Model):
    _inherit = 'payment.token'

    @api.model
    def stripe_create(self, values):
        token = values.get('stripe_token')
        payment_acquirer = self.env['payment.acquirer'].browse(values.get('acquirer_id'))
        stripe.api_key = payment_acquirer.stripe_publishable_key
        partner = self.env['res.partner'].browse(values['partner_id'])
        # when asking to create a token on Stripe servers
        if values.get('iban'):
            if partner.email:
                source = stripe.Source.create(
                    type='sepa_debit',
                    sepa_debit={'iban': values['iban']},
                    currency='eur',
                    owner={
                        'name': partner.name,
                        'email': partner.email,
                    },
                    mandate={
                        'notification_method': 'email',
                    }
                )
            else:
                source = stripe.Source.create(
                    type='sepa_debit',
                    sepa_debit={'iban': values['iban']},
                    currency='eur',
                    owner={
                        'name': partner.name,
                    }
                )
            token = source
            description = values['iban']
        else:
            partner_id = self.env['res.partner'].browse(values['partner_id'])
            description = 'Partner: %s (id: %s)' % (partner_id.name, partner_id.id)

        if not token:
            raise Exception('stripe_create: No token provided!')

        res = self._stripe_create_customer(token, description, payment_acquirer.id)

        # pop credit card info to info sent to create
        for field_name in ["iban",]:
            values.pop(field_name, None)
        return res

    def _stripe_create_customer(self, token, description=None, acquirer_id=None):
        if token.get('error'):
            _logger.error('payment.token.stripe_create_customer: Token error:\n%s', pprint.pformat(token['error']))
            raise Exception(token['error']['message'])

        if token['object'] != 'source':
            _logger.error('payment.token.stripe_create_customer: Cannot create a customer for object type "%s"', token.get('object'))
            raise Exception('We are unable to process your credit card information.')

        if token['type'] != 'sepa_debit':
            _logger.error('payment.token.stripe_create_customer: Cannot create a customer for token type "%s"', token.get('type'))
            raise Exception('We are unable to process your credit card information.')

        payment_acquirer = self.env['payment.acquirer'].browse(acquirer_id or self.acquirer_id.id)
        stripe.api_key = payment_acquirer.stripe_publishable_key

        customer = stripe.Customer.create(
            email=token.get('owner')['email'],
            source=token['id'],
        )

        if customer.get('error'):
            _logger.error('payment.token.stripe_create_customer: Customer error:\n%s', pprint.pformat(customer['error']))
            raise Exception(customer['error']['message'])

        values = {
            'acquirer_ref': customer['id'],
            'name': token['id'],
            'short_name': 'XXXXXXXXXXXX%s ' % (token['sepa_debit']['last4'],)
        }

        return values
