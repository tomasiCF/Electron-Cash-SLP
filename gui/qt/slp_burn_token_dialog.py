import copy
import datetime
from functools import partial
import json
import threading
import sys, traceback

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

from electroncash.address import Address, PublicKey
from electroncash.bitcoin import base_encode, TYPE_ADDRESS
from electroncash.i18n import _
from electroncash.plugins import run_hook

from .util import *

from electroncash.util import bfh, format_satoshis_nofloat, format_satoshis_plain_nofloat, NotEnoughFunds, ExcessiveFee, finalization_print_error
from electroncash.transaction import Transaction
from electroncash.slp import SlpMessage, SlpParsingError, SlpMessage, SlpNoMintingBatonFound, SlpUnsupportedSlpTokenType, SlpInvalidOutputMessage, buildSendOpReturnOutput_V1

from .amountedit import SLPAmountEdit
from .transaction_dialog import show_transaction

from electroncash import networks, util

dialogs = []

class SlpBurnTokenDialog(QDialog, MessageBoxMixin):

    def __init__(self, main_window, token_id_hex, token_name):
        QDialog.__init__(self, parent=None)
        from .main_window import ElectrumWindow

        assert isinstance(main_window, ElectrumWindow)
        main_window._slp_dialogs.add(self)
        finalization_print_error(self)  # Track object lifecycle

        self.main_window = main_window
        self.wallet = main_window.wallet
        self.network = main_window.network
        self.app = main_window.app

        if self.main_window.gui_object.warn_if_no_network(self.main_window):
            return
        
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
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)
        b.clicked.connect(self.close)
        b.setDefault(True)
        hbox.addWidget(self.cancel_button)

        hbox.addStretch(1)

        self.preview_button = EnterButton(_("Preview"), self.do_preview)
        self.import_burn_tx_file_button = EnterButton(_("Import file..."), self.do_process_from_file)
        self.import_burn_tx_text_button = EnterButton(_("Import hex..."), self.do_process_from_text)
        self.burn_button = b = QPushButton(_("Burn Tokens"))
        b.clicked.connect(self.burn_token)
        self.burn_button.setAutoDefault(False)
        self.burn_button.setDefault(False)
        hbox.addWidget(self.import_burn_tx_file_button)
        hbox.addWidget(self.import_burn_tx_text_button)
        hbox.addWidget(self.preview_button)
        hbox.addWidget(self.burn_button)

        dialogs.append(self)
        self.show()
        self.token_qty_e.setFocus()

    def burn_max(self):
        #self.max_button.setChecked(True)
        self.token_qty_e.setAmount(self.wallet.get_slp_token_balance(self.token_id_e.text(), self.main_window.config)[3])

    def do_preview(self):
        self.burn_token(preview=True)

    def read_tx_from_file(self, *, fileName = None):
        fileName = fileName or self.getOpenFileName(_("Select your transaction file"), "*.txn")
        if not fileName:
            return
        try:
            with open(fileName, "r", encoding='utf-8') as f:
                file_content = f.read()
            file_content = file_content.strip()
            tx_file_dict = json.loads(str(file_content))
        except (ValueError, IOError, OSError, json.decoder.JSONDecodeError) as reason:
            self.show_critical(_("Electron Cash was unable to open your transaction file") + "\n" + str(reason), title=_("Unable to read file or no transaction found"))
            return
        tx = self.tx_from_text(file_content)
        return tx

    def do_process_from_file(self, *, fileName = None):
        from electroncash.transaction import SerializationError
        try:
            tx = self.read_tx_from_file(fileName=fileName)
            if tx:
                self.burn_token(preview=True, multisig_tx_to_sign=tx)
        except SerializationError as e:
            self.show_critical(_("Electron Cash was unable to deserialize the transaction:") + "\n" + str(e))

    def tx_from_text(self, txt):
        from electroncash.transaction import tx_from_str
        try:
            txt_tx = tx_from_str(txt)
            tx = Transaction(txt_tx, sign_schnorr=self.wallet.is_schnorr_enabled())
            tx.deserialize()
            if self.wallet:
                my_coins = self.wallet.get_spendable_coins(None, self.main_window.config)
                my_outpoints = [vin['prevout_hash'] + ':' + str(vin['prevout_n']) for vin in my_coins]
                for i, txin in enumerate(tx.inputs()):
                    outpoint = txin['prevout_hash'] + ':' + str(txin['prevout_n'])
                    if outpoint in my_outpoints:
                        my_index = my_outpoints.index(outpoint)
                        tx._inputs[i]['value'] = my_coins[my_index]['value']
            return tx
        except:
            traceback.print_exc(file=sys.stderr)
            self.show_critical(_("Electron Cash was unable to parse your transaction"))
            return    
    
    def do_process_from_text(self):
        from electroncash.transaction import SerializationError
        text = text_dialog(self, _('Input raw transaction'), _("Transaction:"), _("Load transaction"))
        if not text:
            return
        try:
            tx = self.tx_from_text(text)
            if tx:
                self.burn_token(preview=True, multisig_tx_to_sign=tx)
        except SerializationError as e:
            self.show_critical(_("Electron Cash was unable to deserialize the transaction:") + "\n" + str(e))

    # custom wrappers for getOpenFileName and getSaveFileName, that remember the path selected by the user
    def getOpenFileName(self, title, filter = ""):
        return __class__.static_getOpenFileName(title=title, filter=filter, config=self.main_window.config, parent=self)

    def getSaveFileName(self, title, filename, filter = ""):
        return __class__.static_getSaveFileName(title=title, filename=filename, filter=filter, config=self.main_window.config, parent=self)

    @staticmethod
    def static_getOpenFileName(*, title, parent=None, config=None, filter=""):
        userdir = os.path.expanduser('~')
        directory = config.get('io_dir', userdir) if config else userdir
        fileName, __ = QFileDialog.getOpenFileName(parent, title, directory, filter)
        if fileName and directory != os.path.dirname(fileName) and config:
            config.set_key('io_dir', os.path.dirname(fileName), True)
        return fileName

    @staticmethod
    def static_getSaveFileName(*, title, filename, parent=None, config=None, filter=""):
        userdir = os.path.expanduser('~')
        directory = config.get('io_dir', userdir) if config else userdir
        path = os.path.join( directory, filename )
        fileName, __ = QFileDialog.getSaveFileName(parent, title, path, filter)
        if fileName and directory != os.path.dirname(fileName) and config:
            config.set_key('io_dir', os.path.dirname(fileName), True)
        return fileName

    def burn_token(self, preview=False, multisig_tx_to_sign=None):
        unfrozen_token_qty = self.wallet.get_slp_token_balance(self.token_id_e.text(), self.main_window.config)[3]
        burn_amt = self.token_qty_e.get_amount()
        if burn_amt == None:
            self.show_message(_("Invalid token quantity entered."))
            return
        if burn_amt > unfrozen_token_qty:
            self.show_message(_("Cannot burn more tokens than the unfrozen amount available."))
            return

        reply = QMessageBox.question(self, "Continue?", "Destroy " + self.token_qty_e.text() + " " + self.token_name.text() + " tokens?", QMessageBox.Yes, QMessageBox.No)
        if reply == QMessageBox.Yes:
            pass
        else:
            return

        outputs = []

        addr = self.wallet.get_unused_address(frozen_ok=False)
        if addr is None:
            if not self.wallet.is_deterministic():
                addr = self.wallet.get_receiving_address()
            else:
                addr = self.wallet.create_new_address(True)

        try:
            slp_coins = self.wallet.get_slp_utxos(
                        self.token_id_e.text(),
                        domain=None,
                        exclude_frozen=True,
                        confirmed_only=self.main_window.config.get('confirmed_only', False),
                        slp_include_invalid=self.token_burn_invalid_cb.isChecked(),
                        slp_include_baton=self.token_burn_baton_cb.isChecked()
                        )
            if multisig_tx_to_sign is None:
                selected_slp_coins = []
                if burn_amt < unfrozen_token_qty:
                    total_amt_added = 0
                    for coin in slp_coins:
                        if coin['token_value'] != "MINT_BATON" and coin['token_validation_state'] == 1:
                            if coin['token_value'] >= burn_amt:
                                selected_slp_coins.append(coin)
                                total_amt_added += coin['token_value']
                                break
                    if total_amt_added < burn_amt:
                        for coin in slp_coins:
                            if coin['token_value'] != "MINT_BATON" and coin['token_validation_state'] == 1:
                                if total_amt_added < burn_amt:
                                    selected_slp_coins.append(coin)
                                    total_amt_added += coin['token_value']
                    if total_amt_added > burn_amt:
                        token_type = self.wallet.token_types[self.token_id_e.text()]['class']
                        slp_op_return_msg = buildSendOpReturnOutput_V1(
                                                self.token_id_e.text(), 
                                                [total_amt_added - burn_amt], 
                                                token_type
                                                )
                        outputs.append(slp_op_return_msg)
                        outputs.append((TYPE_ADDRESS, addr, 546))
                else:
                    for coin in slp_coins:
                        if coin['token_value'] != "MINT_BATON" and coin['token_validation_state'] == 1:
                            selected_slp_coins.append(coin)
            else:
                selected_slp_coins = []
                total_burn_amt = 0
                try:
                    slp_msg = SlpMessage.parseSlpOutputScript(multisig_tx_to_sign.outputs()[0][1])
                except SlpParsingError:
                    slp_msg = None
                if slp_msg and slp_msg.op_return_fields['token_id_hex'] != self.token_id_e.text():
                    self.show_message(_("Token id in the imported transaction is not correct.")+\
                                            _("\n\nImported Token ID: ") + slp_msg.op_return_fields['token_id_hex'] + \
                                            _("\n\nDesired Token ID: ") + self.token_id_e.text())
                    return
                for txo in multisig_tx_to_sign.inputs():
                    addr = txo['address']
                    prev_out = txo['prevout_hash']
                    prev_n = txo['prevout_n']
                    slp_txo = None
                    try:
                        for coin in slp_coins:
                            if coin['prevout_hash'] == prev_out \
                                and coin['prevout_n'] == prev_n \
                                and coin['token_value'] != "MINT_BATON":
                                selected_slp_coins.append(coin)
                                total_burn_amt += coin['token_value']
                    except KeyError:
                        pass
                if slp_msg:
                    total_burn_amt -= sum(slp_msg.op_return_fields['token_output'])
                if total_burn_amt > burn_amt:
                    if slp_msg:
                        self.show_message(_("Amount burned in transaction does not match the amount specified."))
                    else:
                        self.show_message(_("Amount burned in transaction does not match the amount specified.") + \
                                        _("\n\nMake sure the Token ID displayed in the Burn Tool dialog matches the token that you are trying to burn."))
                    return

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

        try:
            if multisig_tx_to_sign is None:
                bch_change = sum(c['value'] for c in selected_slp_coins)
                outputs.append((TYPE_ADDRESS, addr, bch_change))
                coins = self.main_window.get_coins()
                fixed_fee = None
                tx = self.main_window.wallet.make_unsigned_transaction(
                                                coins,
                                                outputs,
                                                self.main_window.config,
                                                fixed_fee,
                                                None,
                                                mandatory_coins=selected_slp_coins
                                                )
            else:
                tx = multisig_tx_to_sign
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
            show_transaction(tx, self.main_window, None, False, self, slp_coins_to_burn=selected_slp_coins, slp_amt_to_burn=burn_amt)
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

        self.main_window.sign_tx_with_password(tx, sign_done, password, slp_coins_to_burn=selected_slp_coins, slp_amt_to_burn=burn_amt)

        self.burn_button.setDisabled(True)
        self.close()

    def closeEvent(self, event):
        super().closeEvent(event)
        event.accept()
        def remove_self():
            try: dialogs.remove(self)
            except ValueError: pass  # wasn't in list.
        QTimer.singleShot(0, remove_self)  # need to do this some time later. Doing it from within this function causes crashes. See #35

    def update(self):
        return
