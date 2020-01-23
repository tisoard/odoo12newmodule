
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PaymentInvoiceLine(models.Model):
    _name = 'payment.invoice.line'

    invoice_id = fields.Many2one('account.invoice', 'Invoice')
    payment_id = fields.Many2one('account.payment', 'Related Payment')
    partner_id = fields.Many2one(related='invoice_id.partner_id', string='Partner')
    amount_total = fields.Float('Amount Total')
    residual = fields.Float('Amount Due')
    amount = fields.Float('Amount To Pay',
        help="Enter amount to pay for this invoice, supports partial payment")
    date_invoice = fields.Date('Invoice Date')
    select = fields.Boolean('Select', help="Click to select the invoice")

    @api.multi
    @api.constrains('amount')
    def _check_amount(self):
        for line in self:
            if line.amount < 0:
                raise UserError(_('Amount to pay can not be less than 0! (Invoice code: %s)')
                    % line.invoice_id.number)
            if line.amount > line.residual:
                raise UserError(_('"Amount to pay" can not be greater than than "Amount '
                                  'Due" ! (Invoice code: %s)')
                                % line.invoice_id.number)

    @api.onchange('invoice_id')
    def onchange_invoice(self):
        if self.invoice_id:
            self.amount_total = self.invoice_id.amount_total
            self.residual = self.invoice_id.residual
        else:
            self.amount_total = 0.0
            self.residual = 0.0

    @api.onchange('select')
    def onchange_select(self):
        if self.select:
            self.amount = self.invoice_id.residual
        else:
            self.amount = 0.0


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    invoice_total = fields.Float('Invoice Total',
        help="Shows total invoice amount selected for this payment")
    invoice_lines = fields.One2many('payment.invoice.line', 'payment_id', 'Invoices',
        help='Please select invoices for this partner for the payment')
    selected_inv_total = fields.Float(compute='compute_selected_invoice_total',
        store=True, string='Assigned Amount')
    balance = fields.Float(compute='_compute_balance', string='Balance')

    @api.multi
    @api.depends('invoice_lines', 'invoice_lines.amount', 'amount')
    def _compute_balance(self):
        for payment in self:
            total = 0.0
            for line in payment.invoice_lines:
                total += line.amount
            if payment.amount >= total:
                balance = payment.amount - total
            else:
                balance = payment.amount - total
            payment.balance = balance

    @api.multi
    @api.depends('invoice_lines', 'invoice_lines.amount', 'amount')
    def compute_selected_invoice_total(self):
        for payment in self:
            total = 0.0
            for line in payment.invoice_lines:
                total += line.amount
            payment.selected_inv_total = total

    @api.onchange('partner_id', 'payment_type')
    def onchange_partner_id(self):
        Invoice = self.env['account.invoice']
        PaymentLine = self.env['payment.invoice.line']
        if self.partner_id:
            partners_list = self.partner_id.child_ids.ids
            partners_list.append(self.partner_id.id)
            line_ids = []
            type = ''
            if self.payment_type == 'outbound':
                type = 'in_invoice'
            elif self.payment_type == 'inbound':
                type = 'out_invoice'
            invoices = Invoice.search([('partner_id', 'in', partners_list),
                ('state', 'in', ('open', )), ('type', '=', type)], order="date_invoice")
            total_amount = 0
            if self.amount > 0:
                total_amount = self.amount
            for invoice in invoices:
                assigned_amount = 0
                if total_amount > 0:
                    if invoice.residual < total_amount:
                        assigned_amount = invoice.residual
                        total_amount -= invoice.residual
                    else:
                        assigned_amount = total_amount
                        total_amount = 0
                data = {
                    'invoice_id': invoice.id,
                    'amount_total': invoice.amount_total,
                    'residual': invoice.residual,
                    'amount': assigned_amount,
                    'date_invoice': invoice.date_invoice,
                }
                line = PaymentLine.create(data)
                line_ids.append(line.id)
                # line_data.append((0, 0, data))
            self.invoice_lines = [(6, 0, line_ids)]
        else:
            if self.invoice_lines:
                for line in self.invoice_lines:
                    line.unlink()
            self.invoice_lines = []

    @api.onchange('invoice_lines')
    def onchange_invoice_lines(self):
        if self.invoice_lines:
            total = 0.0
            for line in self.invoice_lines:
                total += line.amount
            self.invoice_total = total
        else:
            self.invoice_total = 0.0
            self.amount = 0.0

    @api.onchange('amount')
    def onchange_amount(self):
        ''' Function to reset/select invoices on the basis of invoice date '''
        if self.amount > 0:
            total_amount = self.amount
            for line in self.invoice_lines:
                if total_amount > 0:
                    if line.residual < total_amount:
                        line.amount = line.residual
                        total_amount -= line.residual
                    else:
                        line.amount = total_amount
                        total_amount = 0
        if (self.amount <= 0):
            for line in self.invoice_lines:
                line.amount = 0.0

    @api.multi
    @api.constrains('amount', 'invoice_lines')
    def _check_invoice_amount(self):
        ''' Function to validate if user has selected more amount invoices than payment '''
        for payment in self:
            total = 0.0
            if payment.invoice_lines:
                for line in payment.invoice_lines:
                    total += line.amount
                if total > payment.amount:
                    raise UserError(_('You cannot select more value invoices than the payment amount'))

    def _create_payment_entry(self, amount):
        """ Create a journal entry corresponding to a payment, if the payment references invoice(s) they are reconciled.
            Return the journal entry.
            OVERRIDDEN: generated multiple journal items for each selected invoice
        """
        aml_obj = self.env['account.move.line'].with_context(check_move_validity=False)
        invoice_currency = False
        if self.invoice_ids and all([x.currency_id == self.invoice_ids[0].currency_id for x in self.invoice_ids]):
            # if all the invoices selected share the same currency, record the paiement in that currency too
            invoice_currency = self.invoice_ids[0].currency_id
        debit, credit, amount_currency, currency_id = aml_obj.with_context(
            date=self.payment_date)._compute_amount_fields(
            amount, self.currency_id, self.company_id.currency_id)

        move = self.env['account.move'].create(self._get_move_vals())
        # Custom Code
        counterpart_aml = False
        invoice_reconcile_amount = 0
        sum_credit, sum_debit = 0, 0
        # Creating invoice wise move lines
        if self.invoice_lines:
            for line in self.invoice_lines:
                if line.amount > 0:
                    inv = line.invoice_id
                    inv_amount = line.amount * (self.payment_type in ('outbound', 'transfer') and 1 or -1)
                    invoice_currency = inv.currency_id
                    debit1, credit1, amount_currency1, currency_id1 = aml_obj.with_context(
                        date=self.payment_date)._compute_amount_fields(
                        inv_amount, self.currency_id, self.company_id.currency_id)
                    counterpart_aml_dict1 = self._get_shared_move_line_vals(
                    debit1, credit1, amount_currency1, move.id, False)
                    counterpart_aml_dict1.update(
                        self._get_counterpart_move_line_vals(False))
                    counterpart_aml_dict1.update({'currency_id': currency_id1})
                    counterpart_aml1 = aml_obj.create(counterpart_aml_dict1)
                    inv.register_payment(counterpart_aml1)
                    invoice_reconcile_amount += inv_amount
                    sum_credit += credit1
                    sum_debit += debit1
            # Creating journal item for remaining payment amount
            remaining_amount = 0
            if self.payment_type in ('outbound', 'transfer'):
                remaining_amount = amount - invoice_reconcile_amount
            else:
                remaining_amount = invoice_reconcile_amount - amount
            if remaining_amount > 0:
                remaining_amount = remaining_amount * (
                    self.payment_type in ('outbound', 'transfer') and 1 or -1)
                debit1, credit1, amount_currency1, currency_id1 = aml_obj.with_context(
                    date=self.payment_date)._compute_amount_fields(
                    remaining_amount, self.currency_id, self.company_id.currency_id)
                counterpart_aml_dict1 = self._get_shared_move_line_vals(
                debit1, credit1, amount_currency1, move.id, False)
                counterpart_aml_dict1.update(
                    self._get_counterpart_move_line_vals(False))
                counterpart_aml_dict1.update({'currency_id': currency_id1})
                counterpart_aml1 = aml_obj.create(counterpart_aml_dict1)
                sum_credit += credit1
                sum_debit += debit1
            # Creating move line for currency exchange/conversion rate difference
            if self.payment_type in ('outbound', 'transfer'):
                amount_diff = debit - sum_debit
            else:
                amount_diff = credit - sum_credit
            if amount_diff > 0:
                conversion = self._get_shared_move_line_vals(0, 0, 0, move.id, False)
                debit_co, credit_co, amount_currency_co, currency_id_co = aml_obj.with_context(
                    date=self.payment_date)._compute_amount_fields(
                    amount_diff, self.currency_id, self.company_id.currency_id)

                conversion['name'] = _('Currency exchange rate difference')
                conversion['account_id'] = amount_diff > 0 and \
                                           self.company_id.currency_exchange_journal_id.default_debit_account_id.id or \
                                           self.company_id.currency_exchange_journal_id.default_credit_account_id.id
                if self.payment_type in ('outbound', 'transfer'):
                    conversion['debit'] = amount_diff
                    conversion['credit'] = 0
                else:
                    conversion['debit'] = 0
                    conversion['credit'] = amount_diff
                conversion['currency_id'] = currency_id_co
                conversion['payment_id'] = self.id
                aml_obj.create(conversion)
        else:
            # Default code
            # Write line corresponding to invoice payment
            counterpart_aml_dict = self._get_shared_move_line_vals(debit, credit, amount_currency, move.id, False)
            counterpart_aml_dict.update(self._get_counterpart_move_line_vals(self.invoice_ids))
            counterpart_aml_dict.update({'currency_id': currency_id})
            counterpart_aml = aml_obj.create(counterpart_aml_dict)
            self.invoice_ids.register_payment(counterpart_aml)

        # Default code
        # Reconcile with the invoices
        if self.payment_difference_handling == 'reconcile' and self.payment_difference:
            writeoff_line = self._get_shared_move_line_vals(0, 0, 0, move.id, False)
            debit_wo, credit_wo, amount_currency_wo, currency_id = aml_obj.with_context(
                date=self.payment_date)._compute_amount_fields(
                self.payment_difference, self.currency_id, self.company_id.currency_id)
            writeoff_line['name'] = _('Counterpart')
            writeoff_line['account_id'] = self.writeoff_account_id.id
            writeoff_line['debit'] = debit_wo
            writeoff_line['credit'] = credit_wo
            writeoff_line['amount_currency'] = amount_currency_wo
            writeoff_line['currency_id'] = currency_id
            writeoff_line = aml_obj.create(writeoff_line)
            if counterpart_aml['debit']:
                counterpart_aml['debit'] += credit_wo - debit_wo
            if counterpart_aml['credit']:
                counterpart_aml['credit'] += debit_wo - credit_wo
            counterpart_aml['amount_currency'] -= amount_currency_wo
        # Write counterpart lines
        if not self.currency_id != self.company_id.currency_id:
            amount_currency = 0
        liquidity_aml_dict = self._get_shared_move_line_vals(credit, debit, -amount_currency, move.id, False)
        liquidity_aml_dict.update(self._get_liquidity_move_line_vals(-amount))
        aml_obj.create(liquidity_aml_dict)
        move.post()
        return move

    @api.one
    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        default = dict(default or {})
        default.update(invoice_lines=[], invoice_total=0.0)
        return super(AccountPayment, self).copy(default)
