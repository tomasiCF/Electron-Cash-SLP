import copy
import datetime
from functools import partial
import json
import threading
import sys

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

from electroncash.address import Address, PublicKey
from electroncash.bitcoin import base_encode, TYPE_ADDRESS
from electroncash.i18n import _
from electroncash.plugins import run_hook

from .util import *

from electroncash.util import bfh, format_satoshis_nofloat, format_satoshis_plain_nofloat, NotEnoughFunds, ExcessiveFee
from electroncash.transaction import Transaction
from electroncash.slp import SlpMessage, SlpNoMintingBatonFound, SlpUnsupportedSlpTokenType, SlpInvalidOutputMessage, buildSendOpReturnOutput_V1

from .amountedit import SLPAmountEdit
from .transaction_dialog import show_transaction

from electroncash import networks

dialogs = []

class SlpBurnTokenDialog(QDialog, MessageBoxMixin):

    def __init__(self, main_window, token_id_hex, token_name):
        QDialog.__init__(self, parent=main_window)

        self.main_window = main_window
        self.wallet = main_window.wallet
        self.network = main_window.network
        self.app = main_window.app

        self.baton_txo = None
        try: 
            self.baton_txo = self.main_window.wallet.get_slp_token_baton(token_id_hex)
        except SlpNoMintingBatonFound:
            pass

        self.setWindowTitle(_("Burn Tokens"))

        vbox = QVBoxLayout()
        self.setLayout(vbox)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        vbox.addLayout(grid)
        row = 0

        grid.addWidget(QLabel(_('Name:')), row, 0)

        self.token_name = QLineEdit()
        self.token_name.setFixedWidth(490)
        self.token_name.setText(token_name)
        self.token_name.setDisabled(True)
        grid.addWidget(self.token_name, row, 1)
        row += 1

        msg = _('Unique identifier for the token.')
        grid.addWidget(HelpLabel(_('Token ID:'), msg), row, 0)

        self.token_id_e = QLineEdit()
        self.token_id_e.setFixedWidth(490)
        self.token_id_e.setText(token_id_hex)
        self.token_id_e.setDisabled(True)
        grid.addWidget(self.token_id_e, row, 1)
        row += 1

        msg = _('The number of decimal places used in the token quantity.')
        grid.addWidget(HelpLabel(_('Decimals:'), msg), row, 0)
        self.token_dec = QDoubleSpinBox()
        decimals = self.main_window.wallet.token_types.get(token_id_hex)['decimals']
        self.token_dec.setRange(0, 9)
        self.token_dec.setValue(decimals)
        self.token_dec.setDecimals(0)
        self.token_dec.setFixedWidth(50)
        self.token_dec.setDisabled(True)
        grid.addWidget(self.token_dec, row, 1)
        row += 1

        hbox = QHBoxLayout()
        msg = _('The number of tokens to be destroyed for this token.')
        grid.addWidget(HelpLabel(_('Burn Amount:'), msg), row, 0)
        name = self.main_window.wallet.token_types.get(token_id_hex)['name']
        self.token_qty_e = SLPAmountEdit(name, int(decimals))
        self.token_qty_e.setFixedWidth(200)
        #self.token_qty_e.textChanged.connect(self.check_token_qty)
        hbox.addWidget(self.token_qty_e)

        self.max_button = EnterButton(_("Max"), self.burn_max)
        self.max_button.setFixedWidth(140)
        #self.max_button.setCheckable(True)
        hbox.addWidget(self.max_button)
        hbox.addStretch(1)
        grid.addLayout(hbox, row, 1)
        row += 1

        hbox = QHBoxLayout()
        vbox.addLayout(hbox)

        self.token_burn_baton_cb = cb = QCheckBox(_("Burn Minting Baton"))
        self.token_burn_baton_cb.setChecked(False)
        self.token_burn_baton_cb.setDisabled(True)
        grid.addWidget(self.token_burn_baton_cb, row, 0)
        if self.baton_txo != None: 
            self.token_burn_baton_cb.setDisabled(False)

        self.token_burn_invalid_cb = cb = QCheckBox(_("Burn invalid SLP transactions for this token"))
        self.token_burn_invalid_cb.setChecked(True)
        grid.addWidget(self.token_burn_invalid_cb, row, 1)
        row += 1

        self.cancel_button = b = QPushButton(_("Cancel"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(False)
        b.clicked.connect(self.close)
        b.setDefault(True)
        hbox.addWidget(self.cancel_button)

        hbox.addStretch(1)

        self.preview_button = EnterButton(_("Preview"), self.do_preview)
        self.burn_button = b = QPushButton(_("Burn Tokens"))
        b.clicked.connect(self.burn_token)
        self.burn_button.setAutoDefault(True)
        self.burn_button.setDefault(True)
        hbox.addWidget(self.preview_button)
        hbox.addWidget(self.burn_button)

        dialogs.append(self)
        self.show()
        self.token_qty_e.setFocus()

    def burn_max(self):
        #self.max_button.setChecked(True)
        self.token_qty_e.setAmount(self.wallet.get_slp_token_balance(self.token_id_e.text())[3])

    def do_preview(self):
        self.burn_token(preview = True)

    def burn_token(self, preview=False):
        unfrozen_token_qty = self.wallet.get_slp_token_balance(self.token_id_e.text())[3]
        burn_amt = self.token_qty_e.get_amount()
        if burn_amt == None or burn_amt == 0:
            self.show_message(_("Invalid token quantity entered."))
            return
        if burn_amt > unfrozen_token_qty:
            self.show_message(_("Cannot burn more tokens than the unfrozen amount available."))
            return

        outputs = []
        slp_coins = self.wallet.get_slp_utxos(
            self.token_id_e.text(), 
            domain=None, exclude_frozen=True, mature=True, confirmed_only=self.main_window.config.get('confirmed_only', False),
            slp_include_invalid=self.token_burn_invalid_cb.isChecked(), slp_include_baton=self.token_burn_baton_cb.isChecked())

        try:
            selected_slp_coins = []
            if burn_amt < unfrozen_token_qty:
                total_amt_added = 0
                for coin in slp_coins:
                    if coin['token_value'] != "MINT_BATON" and coin['token_validation_state'] == 1:
                        if coin['token_value'] >= burn_amt:
                            selected_slp_coins.append(coin)
                            total_amt_added+=coin['token_value']
                            break
                if total_amt_added < burn_amt:
                    for coin in slp_coins:
                        if coin['token_value'] != "MINT_BATON" and coin['token_validation_state'] == 1:
                            if total_amt_added < burn_amt:
                                selected_slp_coins.append(coin)
                                total_amt_added+=coin['token_value']
                if total_amt_added > burn_amt:
                    slp_op_return_msg = buildSendOpReturnOutput_V1(self.token_id_e.text(), [total_amt_added - burn_amt])
                    outputs.append(slp_op_return_msg)
                    outputs.append((TYPE_ADDRESS, self.wallet.get_unused_address(), 546))
            else:  
                for coin in slp_coins:
                    if coin['token_value'] != "MINT_BATON" and coin['token_validation_state'] == 1:
                        selected_slp_coins.append(coin)

        except OPReturnTooLarge:
            self.show_message(_("Optional string text causiing OP_RETURN greater than 223 bytes."))
            return
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return
    
        if self.token_burn_baton_cb.isChecked():
            for coin in slp_coins:
                if coin['token_value'] == "MINT_BATON" and coin['token_validation_state'] == 1:
                    selected_slp_coins.append(coin)

        if self.token_burn_invalid_cb.isChecked():
            for coin in slp_coins:
                if coin['token_validation_state'] != 1:
                    selected_slp_coins.append(coin)

        bch_change = sum(c['value'] for c in selected_slp_coins)
        outputs.append((TYPE_ADDRESS, self.wallet.get_unused_address(), bch_change))

        coins = self.main_window.get_coins()
        fixed_fee = None

        try:
            tx = self.main_window.wallet.make_unsigned_transaction(coins, outputs, self.main_window.config, fixed_fee, None, mandatory_coins=selected_slp_coins)
        except NotEnoughFunds:
            self.show_message(_("Insufficient funds"))
            return
        except ExcessiveFee:
            self.show_message(_("Your fee is too high.  Max is 50 sat/byte."))
            return
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        if preview:
            show_transaction(tx, self.main_window, None, False, self)
            return

        msg = []

        if self.main_window.wallet.has_password():
            msg.append("")
            msg.append(_("Enter your password to proceed"))
            password = self.main_window.password_dialog('\n'.join(msg))
            if not password:
                return
        else:
            password = None

        tx_desc = None

        def sign_done(success):
            if success:
                if not tx.is_complete():
                    show_transaction(tx, self.main_window, None, False, self)
                    self.main_window.do_clear()
                else:
                    self.main_window.broadcast_transaction(tx, tx_desc)

        self.main_window.sign_tx_with_password(tx, sign_done, password)

        self.burn_button.setDisabled(True)
        self.close()

    def closeEvent(self, event):
        event.accept()
        try:
            dialogs.remove(self)
        except ValueError:
            pass

    def update(self):
        return
