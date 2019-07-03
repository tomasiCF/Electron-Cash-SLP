#!/usr/bin/env python3
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import re
import sys, time, threading
import os, json, traceback
import shutil
import csv
from decimal import Decimal as PyDecimal  # Qt 5.12 also exports Decimal
import base64
from functools import partial
from collections import OrderedDict

from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *

from electroncash import keystore, get_config
from electroncash.address import Address, ScriptOutput
from electroncash.bitcoin import COIN, TYPE_ADDRESS, TYPE_SCRIPT
from electroncash import networks
from electroncash.plugins import run_hook
from electroncash.i18n import _
from electroncash.util import (format_time, format_satoshis, PrintError,
                               format_satoshis_plain, format_satoshis_plain_nofloat,
                               NotEnoughFunds, NotEnoughFundsSlp, NotEnoughUnfrozenFundsSlp, ExcessiveFee,
                               UserCancelled, InvalidPassword, bh2u, bfh,
                               format_fee_satoshis, Weak, print_error)
import electroncash.web as web
from electroncash import Transaction
from electroncash import util, bitcoin, commands
from electroncash import paymentrequest
from electroncash.wallet import Multisig_Wallet, sweep_preparations
try:
    from electroncash.plot import plot_history
except:
    plot_history = None
import electroncash.web as web

from .amountedit import AmountEdit, BTCAmountEdit, MyLineEdit, BTCkBEdit, BTCSatsByteEdit
from .qrcodewidget import QRCodeWidget, QRDialog
from .qrtextedit import ShowQRTextEdit, ScanQRTextEdit
from .transaction_dialog import show_transaction
from .fee_slider import FeeSlider
from .popup_widget import ShowPopupLabel, KillPopupLabel, PopupWidget

from .util import *

import electroncash.slp as slp
from electroncash import slp_validator_0x01
from electroncash.slp_coinchooser import SlpCoinChooser
from electroncash.slp_checker import SlpTransactionChecker
from .amountedit import SLPAmountEdit
from electroncash.util import format_satoshis_nofloat
from .slp_create_token_genesis_dialog import SlpCreateTokenGenesisDialog
from .bfp_download_file_dialog import BfpDownloadFileDialog
from .bfp_upload_file_dialog import BitcoinFilesUploadDialog

class StatusBarButton(QPushButton):
    def __init__(self, icon, tooltip, func):
        QPushButton.__init__(self, icon, '')
        self.setToolTip(tooltip)
        self.setFlat(True)
        self.setMaximumWidth(25)
        self.clicked.connect(self.onPress)
        self.func = func
        self.setIconSize(QSize(25,25))
        self.setCursor(Qt.PointingHandCursor)

    def onPress(self, checked=False):
        '''Drops the unwanted PyQt5 "checked" argument'''
        self.func()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Return:
            self.func()

from electroncash.paymentrequest import PR_PAID


class ElectrumWindow(QMainWindow, MessageBoxMixin, PrintError):

    # Note: self.clean_up_connections automatically detects signals named XXX_signal and disconnects them on window close.
    payment_request_ok_signal = pyqtSignal()
    payment_request_error_signal = pyqtSignal()
    new_fx_quotes_signal = pyqtSignal()
    new_fx_history_signal = pyqtSignal()
    network_signal = pyqtSignal(str, object)
    alias_received_signal = pyqtSignal()
    computing_privkeys_signal = pyqtSignal()
    show_privkeys_signal = pyqtSignal()
    cashaddr_toggled_signal = pyqtSignal()
    slp_validity_signal = pyqtSignal(object, object)
    history_updated_signal = pyqtSignal()
    labels_updated_signal = pyqtSignal() # note this signal occurs when an explicit update_labels() call happens. Interested GUIs should also listen for history_updated_signal as well which also indicates labels may have changed.
    on_timer_signal = pyqtSignal()  # functions wanting to be executed from timer_actions should connect to this signal, preferably via Qt.DirectConnection

    status_icon_dict = dict()  # app-globel cache of "status_*" -> QIcon instances (for update_status() speedup)

    def __init__(self, gui_object, wallet):
        QMainWindow.__init__(self)

        self.gui_object = gui_object
        self.wallet = wallet
        self.config = config = gui_object.config
        self.is_slp_wallet = "slp_" in self.wallet.storage.get('wallet_type', '')
        self.non_slp_wallet_warning_shown = False
        self.force_use_single_change_addr = _('Change addresses behavior is not customizable for SLP wallets') if self.is_slp_wallet else False

        self.network = gui_object.daemon.network
        self.fx = gui_object.daemon.fx
        self.invoices = wallet.invoices
        self.contacts = wallet.contacts
        self.tray = gui_object.tray
        self.app = gui_object.app
        self.cleaned_up = False
        self.payment_request = None
        self.checking_accounts = False
        self.qr_window = None
        self.not_enough_funds = False
        self.not_enough_funds_slp = False
        self.not_enough_unfrozen_funds_slp = False
        self.op_return_toolong = False
        self.internalpluginsdialog = None
        self.externalpluginsdialog = None
        self.require_fee_update = False
        self.tl_windows = []
        self.tx_external_keypairs = {}
        self._tx_dialogs = Weak.Set()
        self.tx_update_mgr = TxUpdateMgr(self)  # manages network callbacks for 'new_transaction' and 'verified2', and collates GUI updates from said callbacks as a performance optimization
        self.is_schnorr_enabled = self.wallet.is_schnorr_enabled  # This is a function -- Support for plugins that may be using the 4.0.3 & 4.0.4 API -- this function used to live in this class, before being moved to Abstract_Wallet.
        self.send_tab_opreturn_widgets, self.receive_tab_opreturn_widgets = [], []  # defaults to empty list

        self.create_status_bar()
        self.need_update = threading.Event()
        self.labels_need_update = threading.Event()

        self.decimal_point = config.get('decimal_point', 8)
        self.fee_unit = config.get('fee_unit', 0)
        self.num_zeros     = int(config.get('num_zeros',0))

        self.completions = QStringListModel()

        self.tabs = tabs = QTabWidget(self)



        self.send_tab = self.create_send_tab()
        self.receive_tab = self.create_receive_tab()
        self.addresses_tab = self.create_addresses_tab()
        self.utxo_tab = self.create_utxo_tab()
        self.console_tab = self.create_console_tab()
        self.contacts_tab = self.create_contacts_tab()
        self.slp_mgt_tab = self.create_slp_mgt_tab()
        self.converter_tab = self.create_converter_tab()
        self.slp_history_tab = self.create_slp_history_tab()
        self.slp_token_id = None
        tabs.addTab(self.create_history_tab(), QIcon(":icons/tab_history.png"), _('History'))
        tabs.addTab(self.send_tab, QIcon(":icons/tab_send.png"), _('Send'))
        tabs.addTab(self.receive_tab, QIcon(":icons/tab_receive.png"), _('Receive'))
        # clears/inits the opreturn widgets
        self.on_toggled_opreturn(bool(self.config.get('enable_opreturn')))

        def add_optional_tab(tabs, tab, icon, description, name, default=False):
            tab.tab_icon = icon
            tab.tab_description = description
            tab.tab_pos = len(tabs)
            tab.tab_name = name
            if self.config.get('show_{}_tab'.format(name), default):
                tabs.addTab(tab, icon, description.replace("&", ""))

        add_optional_tab(tabs, self.addresses_tab, QIcon(":icons/tab_addresses.png"), _("&Addresses"), "addresses")
        add_optional_tab(tabs, self.utxo_tab, QIcon(":icons/tab_coins.png"), _("Co&ins"), "utxo")
        add_optional_tab(tabs, self.contacts_tab, QIcon(":icons/tab_contacts.png"), _("Con&tacts"), "contacts")
        add_optional_tab(tabs, self.converter_tab, QIcon(":icons/tab_converter.svg"), _("Address Converter"), "converter", True)
        add_optional_tab(tabs, self.console_tab, QIcon(":icons/tab_console.png"), _("Con&sole"), "console")
        if self.is_slp_wallet:
            add_optional_tab(tabs, self.slp_mgt_tab, QIcon(":icons/tab_slp_icon.png"), _("Tokens"), "tokens")
            add_optional_tab(tabs, self.slp_history_tab, QIcon(":icons/tab_slp_icon.png"), _("SLP History"), "slp_history", True)


        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCentralWidget(tabs)

        if self.config.get("is_maximized"):
            self.showMaximized()

        self.init_menubar()

        wrtabs = Weak(tabs)
        QShortcut(QKeySequence("Ctrl+W"), self, self.close)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
        QShortcut(QKeySequence("Ctrl+R"), self, self.update_wallet)
        QShortcut(QKeySequence("Ctrl+PgUp"), self, lambda: wrtabs.setCurrentIndex((wrtabs.currentIndex() - 1)%wrtabs.count()))
        QShortcut(QKeySequence("Ctrl+PgDown"), self, lambda: wrtabs.setCurrentIndex((wrtabs.currentIndex() + 1)%wrtabs.count()))

        for i in range(wrtabs.count()):
            QShortcut(QKeySequence("Alt+" + str(i + 1)), self, lambda i=i: wrtabs.setCurrentIndex(i))

        self.cashaddr_toggled_signal.connect(self.update_cashaddr_icon)
        self.payment_request_ok_signal.connect(self.payment_request_ok)
        self.payment_request_error_signal.connect(self.payment_request_error)
        self.gui_object.update_available_signal.connect(self.on_update_available)  # shows/hides the update_available_button, emitted by update check mechanism when a new version is available
        self.history_list.setFocus(True)
        self.slp_history_list.setFocus(True)

        # update fee slider in case we missed the callback
        self.fee_slider.update()
        self.load_wallet(wallet)

        if self.network:
            self.network_signal.connect(self.on_network_qt)
            interests = ['blockchain_updated', 'wallet_updated',
                         'new_transaction', 'status', 'banner', 'verified2',
                         'fee']
            # To avoid leaking references to "self" that prevent the
            # window from being GC-ed when closed, callbacks should be
            # methods of this class only, and specifically not be
            # partials, lambdas or methods of subobjects.  Hence...
            self.network.register_callback(self.on_network, interests)
            # set initial message
            self.console.showMessage(self.network.banner)
            self.network.register_callback(self.on_quotes, ['on_quotes'])
            self.network.register_callback(self.on_history, ['on_history'])
            self.new_fx_quotes_signal.connect(self.on_fx_quotes)
            self.new_fx_history_signal.connect(self.on_fx_history)

        gui_object.timer.timeout.connect(self.timer_actions)
        self.fetch_alias()

    _first_shown = True
    def showEvent(self, event):
        super().showEvent(event)
        if event.isAccepted() and self._first_shown:
            self._first_shown = False
            weakSelf = Weak.ref(self)

            # do this immediately after this event handler finishes -- noop on everything but linux
            QTimer.singleShot(0, lambda: weakSelf() and weakSelf().gui_object.lin_win_maybe_show_highdpi_caveat_msg(weakSelf()))

    def update_token_type_combo(self):
        self.token_type_combo.clear()
        self.receive_token_type_combo.clear()
        self.token_type_combo.addItem(QIcon(':icons/tab_coins.png'), 'None', None)
        self.receive_token_type_combo.addItem(QIcon(':icons/tab_coins.png'), 'None', None)

        try:
            token_types = self.wallet.token_types
        except AttributeError:
            pass
        else:
            sorted_items = sorted(token_types.items(), key=lambda x:x[1]['name'])
            for token_id, i in sorted_items:
                if i['decimals'] != '?':
                    self.token_type_combo.addItem(QIcon(':icons/tab_slp_icon.png'),i['name'], token_id)
                    self.receive_token_type_combo.addItem(QIcon(':icons/tab_slp_icon.png'),i['name'], token_id)

    def on_history(self, event, *args):
        # NB: event should always be 'on_history'
        if not args or args[0] is self.wallet:
            self.new_fx_history_signal.emit()

    @rate_limited(3.0) # Rate limit to no more than once every 3 seconds
    def on_fx_history(self):
        if self.cleaned_up: return
        self.history_list.refresh_headers()
        self.history_list.update()
        self.address_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history

    def on_quotes(self, b):
        self.new_fx_quotes_signal.emit()

    @rate_limited(3.0) # Rate limit to no more than once every 3 seconds
    def on_fx_quotes(self):
        if self.cleaned_up: return
        self.update_status()
        # Refresh edits with the new rate
        edit = self.fiat_send_e if self.fiat_send_e.is_last_edited else self.amount_e
        edit.textEdited.emit(edit.text())
        edit = self.fiat_receive_e if self.fiat_receive_e.is_last_edited else self.receive_amount_e
        edit.textEdited.emit(edit.text())
        # History tab needs updating if it used spot
        if self.fx.history_used_spot:
            self.history_list.update()
            self.history_updated_signal.emit() # inform things like address_dialog that there's a new history

    def toggle_tab(self, tab, forceStatus = 0):

        # forceStatus = 0 , do nothing
        # forceStatus = 1 , force Show
        # forceStatus = 2 , force hide
        if forceStatus==1:
            show=True
        elif forceStatus==2:
            show=False
        else:
            show = not self.config.get('show_{}_tab'.format(tab.tab_name), False)
        self.config.set_key('show_{}_tab'.format(tab.tab_name), show)
        item_format = _("Hide {tab_description}") if show else _("Show {tab_description}")
        item_text = item_format.format(tab_description=tab.tab_description)
        tab.menu_action.setText(item_text)
        if show:
            # Find out where to place the tab
            index = len(self.tabs)
            for i in range(len(self.tabs)):
                try:
                    if tab.tab_pos < self.tabs.widget(i).tab_pos:
                        index = i
                        break
                except AttributeError:
                    pass
            self.tabs.insertTab(index, tab, tab.tab_icon, tab.tab_description.replace("&", ""))
        else:
            i = self.tabs.indexOf(tab)
            self.tabs.removeTab(i)

    def push_top_level_window(self, window):
        '''Used for e.g. tx dialog box to ensure new dialogs are appropriately
        parented.  This used to be done by explicitly providing the parent
        window, but that isn't something hardware wallet prompts know.'''
        self.tl_windows.append(window)

    def pop_top_level_window(self, window):
        self.tl_windows.remove(window)

    def top_level_window(self):
        '''Do the right thing in the presence of tx dialog windows'''
        override = self.tl_windows[-1] if self.tl_windows else None
        return self.top_level_window_recurse(override)

    def diagnostic_name(self):
        return "%s/%s" % (PrintError.diagnostic_name(self), self.wallet.basename())

    def is_hidden(self):
        return self.isMinimized() or self.isHidden()

    def show_or_hide(self):
        if self.is_hidden():
            self.bring_to_top()
        else:
            self.hide()

    def bring_to_top(self):
        self.show()
        self.raise_()

    def on_error(self, exc_info):
        if not isinstance(exc_info[1], UserCancelled):
            try:
                traceback.print_exception(*exc_info)
            except OSError:
                # Issue #662, user got IO error.
                # We want them to still get the error displayed to them.
                pass
            self.show_error(str(exc_info[1]))

    def on_network(self, event, *args):
        #self.print_error("on_network:", event, *args)
        if event == 'wallet_updated':
            if args[0] is self.wallet:
                self.need_update.set()
        elif event == 'blockchain_updated':
            self.need_update.set()
        elif event == 'new_transaction':
            self.tx_update_mgr.notif_add(args)  # added only if this wallet's tx
            if args[1] is self.wallet:
                self.network_signal.emit(event, args)
        elif event == 'verified2':
            self.tx_update_mgr.verif_add(args)  # added only if this wallet's tx
        elif event in ['status', 'banner', 'fee']:
            # Handle in GUI thread
            self.network_signal.emit(event, args)
        else:
            self.print_error("unexpected network message:", event, args)

    def on_network_qt(self, event, args=None):
        if self.cleaned_up: return
        # Handle a network message in the GUI thread
        if event == 'status':
            self.update_status()
        elif event == 'banner':
            self.console.showMessage(args[0])
        elif event == 'fee':
            pass
        elif event == 'new_transaction':
            self.check_and_reset_receive_address_if_needed()
        else:
            self.print_error("unexpected network_qt signal:", event, args)

    def fetch_alias(self):
        self.alias_info = None
        alias = self.config.get('alias')
        if alias:
            alias = str(alias)
            def f():
                self.alias_info = self.contacts.resolve_openalias(alias)
                self.alias_received_signal.emit()
            t = threading.Thread(target=f)
            t.setDaemon(True)
            t.start()

    def _close_wallet(self):
        if self.wallet:
            self.print_error('close_wallet', self.wallet.storage.path)
            self.wallet.thread = None

        run_hook('close_wallet', self.wallet)

    def load_wallet(self, wallet):
        wallet.thread = TaskThread(self, self.on_error, name = wallet.diagnostic_name() + '/Wallet')
        self.wallet.ui_emit_validity_updated = self.slp_validity_signal.emit
        self.update_recently_visited(wallet.storage.path)
        # address used to create a dummy transaction and estimate transaction fee
        self.history_list.update()
        self.address_list.update()
        self.utxo_list.update()
        self.need_update.set()
        # update menus
        self.seed_menu.setEnabled(self.wallet.has_seed())
        self.update_lock_icon()
        self.update_buttons_on_seed()
        self.update_console()
        self.clear_receive_tab()
        self.request_list.update()

        # Set up SLP proxy here -- needs to be done before wallet.activate_slp is called.
        slp_validator_0x01.setup_config(self.config)

        if self.is_slp_wallet:
            self.wallet.activate_slp()
            self.slp_history_list.update()
            self.token_list.update()
            self.update_token_type_combo()

        self.tabs.show()
        self.init_geometry()
        if self.config.get('hide_gui') and self.tray.isVisible():
            self.hide()
        else:
            self.show()
            if self._is_invalid_testnet_wallet():
                self.gui_object.daemon.stop_wallet(self.wallet.storage.path)
                self._rebuild_history_action.setEnabled(False)
                self._warn_if_invalid_testnet_wallet()
        self.watching_only_changed()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history
        if self.is_slp_wallet:
            self.toggle_cashaddr(2, True)
            self.toggle_tab(self.slp_mgt_tab, 1)
            self.toggle_tab(self.slp_history_tab, 1)
        else:
            self.toggle_cashaddr(1, True)
        self.update_receive_address_widget()
        self.address_list.update()
        self.utxo_list.update()
        self.slp_mgt_tab.update()
        self.slp_history_tab.update()
        self.update_cashaddr_icon()
        run_hook('load_wallet', wallet, self)

    def init_geometry(self):
        winpos = self.wallet.storage.get("winpos-qt")
        try:
            screen = self.app.desktop().screenGeometry()
            assert screen.contains(QRect(*winpos))
            self.setGeometry(*winpos)
        except:
            self.print_error("using default geometry")
            self.setGeometry(100, 100, 840, 400)

    def watching_only_changed(self):
        title = '%s %s  -  %s' % (networks.net.TITLE,
                                  self.wallet.electrum_version,
                                  self.wallet.basename())
        extra = [self.wallet.storage.get('wallet_type', '?')]
        if self.wallet.is_watching_only():
            self.warn_if_watching_only()
            extra.append(_('watching only'))
        title += '  [%s]'% ', '.join(extra)
        self.setWindowTitle(title)
        self.password_menu.setEnabled(self.wallet.can_change_password())
        self.import_privkey_menu.setVisible(self.wallet.can_import_privkey())
        self.import_address_menu.setVisible(self.wallet.can_import_address())
        self.export_menu.setEnabled(self.wallet.can_export())

    def warn_if_watching_only(self):
        if self.wallet.is_watching_only():
            msg = ' '.join([
                _("This wallet is watching-only."),
                _("This means you will not be able to spend Bitcoin Cash with it."),
                _("Make sure you own the seed phrase or the private keys, before you request Bitcoin Cash to be sent to this wallet.")
            ])
            self.show_warning(msg, title=_('Information'))

    def _is_invalid_testnet_wallet(self):
        if not networks.net.TESTNET:
            return False
        is_old_bad = False
        xkey = ((hasattr(self.wallet, 'get_master_public_key') and self.wallet.get_master_public_key())
                or None)
        if xkey:
            from electroncash.bitcoin import deserialize_xpub, InvalidXKeyFormat
            try:
                xp = deserialize_xpub(xkey)
            except InvalidXKeyFormat:
                is_old_bad = True
        return is_old_bad

    def _warn_if_invalid_testnet_wallet(self):
        ''' This was added after the upgrade from the bad xpub testnet wallets
        to the good tpub testnet wallet format in version 3.3.6. See #1164.
        We warn users if they are using the bad wallet format and instruct
        them on how to upgrade their wallets.'''
        is_old_bad = self._is_invalid_testnet_wallet()
        if is_old_bad:
            msg = ' '.join([
                _("This testnet wallet has an invalid master key format."),
                _("(Old versions of Electron Cash before 3.3.6 produced invalid testnet wallets)."),
                '<br><br>',
                _("In order to use this wallet without errors with this version of EC, please <b>re-generate this wallet from seed</b>."),
                "<br><br><em><i>~SPV stopped~</i></em>"
            ])
            self.show_critical(msg, title=_('Invalid Master Key'), rich_text=True)
        return is_old_bad

    def _warn_slp_prefers_slp_wallets_if_not_slp_wallet(self):
        if not self.is_slp_wallet and not self.non_slp_wallet_warning_shown:
            msg = '\n\n'.join([
                _("WARNING: SLP Tokens Disabled."),
                _("SLP tokens were detected in this older style wallet file and this version does not allow use of SLP tokens for your protection."),
                _("Please install version 3.4.6 to create a new SLP wallet file and then transfer the tokens from this wallet file to the new 3.4.6 style wallet file."),
                _("Why? This is because Electron Cash SLP versions 3.4.3 and later all include a significant security improvement for SLP tokens. That is, all standard wallet files created with 3.4.3 and later use BIP-44 key derivation path m/44'/245' to reduce the risk of burning SLP tokens.  Taking no action could result in burning your tokens if this wallet's seed is imported into a non-SLP aware wallet."),
                _('''If you're wondering "what do I have to do?":'''),
                _("If you want to recover the SLP tokens in this wallet file you need to install version 3.4.6 of this software and follow the instructions provided above.")
            ])
            self.show_warning(msg, title=_("SLP Tokens Detected in a Non-SLP Wallet"))
            self.non_slp_wallet_warning_shown = True

    def open_wallet(self):
        try:
            wallet_folder = self.get_wallet_folder()
        except FileNotFoundError as e:
            self.show_error(str(e))
            return
        if not os.path.exists(wallet_folder):
            wallet_folder = None
        filename, __ = QFileDialog.getOpenFileName(self, "Select your wallet file", wallet_folder)
        if not filename:
            return
        if filename.lower().endswith('.txn'):
            # they did File -> Open on a .txn, just do that.
            self.do_process_from_file(fileName=filename)
            return
        self.gui_object.new_window(filename)


    def backup_wallet(self):
        path = self.wallet.storage.path
        wallet_folder = os.path.dirname(path)
        filename, __ = QFileDialog.getSaveFileName(self, _('Enter a filename for the copy of your wallet'), wallet_folder)
        if not filename:
            return

        new_path = os.path.join(wallet_folder, filename)
        if new_path != path:
            try:
                # Copy file contents
                shutil.copyfile(path, new_path)

                # Copy file attributes if possible
                # (not supported on targets like Flatpak documents)
                try:
                    shutil.copystat(path, new_path)
                except (IOError, os.error):
                    pass

                self.show_message(_("A copy of your wallet file was created in")+" '%s'" % str(new_path), title=_("Wallet backup created"))
            except (IOError, os.error) as reason:
                self.show_critical(_("Electron Cash was unable to copy your wallet file to the specified location.") + "\n" + str(reason), title=_("Unable to create backup"))

    def update_recently_visited(self, filename):
        recent = self.config.get('recently_open', [])
        try:
            sorted(recent)
        except:
            recent = []
        if filename in recent:
            recent.remove(filename)
        recent.insert(0, filename)
        recent2 = []
        for k in recent:
            if os.path.exists(k):
                recent2.append(k)
        recent = recent2[:5]
        self.config.set_key('recently_open', recent)
        self.recently_visited_menu.clear()
        gui_object = self.gui_object
        for i, k in enumerate(sorted(recent)):
            b = os.path.basename(k)
            def loader(k):
                return lambda: gui_object.new_window(k)
            self.recently_visited_menu.addAction(b, loader(k)).setShortcut(QKeySequence("Ctrl+%d"%(i+1)))
        self.recently_visited_menu.setEnabled(len(recent))

    def get_wallet_folder(self):
        return self.gui_object.get_wallet_folder()

    def new_wallet(self):
        try:
            full_path = self.gui_object.get_new_wallet_path()
        except FileNotFoundError as e:
            self.show_error(str(e))
            return
        self.gui_object.start_new_window(full_path, None)

    def init_menubar(self):
        menubar = QMenuBar()
        menubar.setObjectName(self.diagnostic_name() + ".QMenuBar")
        destroyed_print_error(menubar)

        file_menu = menubar.addMenu(_("&File"))
        self.recently_visited_menu = file_menu.addMenu(_("&Recently open"))
        file_menu.addAction(_("&Open"), self.open_wallet).setShortcut(QKeySequence.Open)
        file_menu.addAction(_("&New/Restore"), self.new_wallet).setShortcut(QKeySequence.New)
        file_menu.addAction(_("&Save Copy"), self.backup_wallet).setShortcut(QKeySequence.SaveAs)
        file_menu.addAction(_("Delete"), self.remove_wallet)
        file_menu.addSeparator()
        file_menu.addAction(_("&Quit"), self.close)

        wallet_menu = menubar.addMenu(_("&Wallet"))
        wallet_menu.addAction(_("&Information"), self.show_master_public_keys)
        wallet_menu.addSeparator()
        self.password_menu = wallet_menu.addAction(_("&Password"), self.change_password_dialog)
        self.seed_menu = wallet_menu.addAction(_("&Seed"), self.show_seed_dialog)
        self.private_keys_menu = wallet_menu.addMenu(_("&Private keys"))
        pk_sweep_action = self.private_keys_menu.addAction(_("&Sweep"), self.sweep_key_dialog)
        pk_sweep_action.setEnabled(False)  # alwqays disable for SLP for now.
        self.import_privkey_menu = self.private_keys_menu.addAction(_("&Import"), self.do_import_privkey)
        self.export_menu = self.private_keys_menu.addAction(_("&Export"), self.export_privkeys_dialog)
        self.import_address_menu = wallet_menu.addAction(_("Import addresses"), self.import_addresses)
        wallet_menu.addSeparator()
        self._rebuild_history_action = wallet_menu.addAction(_("&Rebuild history"), self.rebuild_history)
        self._scan_beyond_gap_action = wallet_menu.addAction(_("&Scan beyond gap..."), self.scan_beyond_gap)
        self._scan_beyond_gap_action.setEnabled(bool(self.wallet.is_deterministic() and self.network))
        wallet_menu.addSeparator()

        labels_menu = wallet_menu.addMenu(_("&Labels"))
        labels_menu.addAction(_("&Import"), self.do_import_labels)
        labels_menu.addAction(_("&Export"), self.do_export_labels)
        contacts_menu = wallet_menu.addMenu(_("Contacts"))
        contacts_menu.addAction(_("&New"), self.new_contact_dialog)
        contacts_menu.addAction(_("Import"), lambda: self.contact_list.import_contacts())
        contacts_menu.addAction(_("Export"), lambda: self.contact_list.export_contacts())
        invoices_menu = wallet_menu.addMenu(_("Invoices"))
        invoices_menu.addAction(_("Import"), lambda: self.invoice_list.import_invoices())
        hist_menu = wallet_menu.addMenu(_("&History"))
        #hist_menu.addAction(_("Plot"), self.plot_history_dialog).setEnabled(plot_history is not None)
        hist_menu.addAction(_("Export"), self.export_history_dialog)

        wallet_menu.addSeparator()
        wallet_menu.addAction(_("Find"), self.toggle_search).setShortcut(QKeySequence("Ctrl+F"))

        def add_toggle_action(view_menu, tab):
            is_shown = self.tabs.indexOf(tab) > -1
            item_format = _("Hide {tab_description}") if is_shown else _("Show {tab_description}")
            item_name = item_format.format(tab_description=tab.tab_description)
            tab.menu_action = view_menu.addAction(item_name, lambda: self.toggle_tab(tab))

        view_menu = menubar.addMenu(_("&View"))
        add_toggle_action(view_menu, self.addresses_tab)
        add_toggle_action(view_menu, self.utxo_tab)
        add_toggle_action(view_menu, self.contacts_tab)
        add_toggle_action(view_menu, self.converter_tab)
        add_toggle_action(view_menu, self.console_tab)
        if self.is_slp_wallet:
            add_toggle_action(view_menu, self.slp_mgt_tab)
            add_toggle_action(view_menu, self.slp_history_tab)

        tools_menu = menubar.addMenu(_("&Tools"))

        # Settings / Preferences are all reserved keywords in OSX using this as work around
        tools_menu.addAction(_("Electron Cash preferences") if sys.platform == 'darwin' else _("Preferences"), self.settings_dialog)
        gui_object = self.gui_object
        weakSelf = Weak(self)
        tools_menu.addAction(_("&Network"), lambda: gui_object.show_network_dialog(weakSelf))
        tools_menu.addAction(_("Optional &Features"), self.internal_plugins_dialog)
        tools_menu.addAction(_("Installed &Plugins"), self.external_plugins_dialog)
        tools_menu.addSeparator()
        tools_menu.addAction(_("&Sign/verify message"), self.sign_verify_message)
        tools_menu.addAction(_("&Encrypt/decrypt message"), self.encrypt_message)
        tools_menu.addSeparator()
        tools_menu.addAction(_("Upload a file using BFP"), lambda: BitcoinFilesUploadDialog(self, None, True, "Upload a File Using BFP"))
        tools_menu.addAction(_("Download a file using BFP"), lambda: BfpDownloadFileDialog(self,))
        tools_menu.addSeparator()

        paytomany_menu = tools_menu.addAction(_("&Pay to many"), self.paytomany)

        raw_transaction_menu = tools_menu.addMenu(_("&Load transaction"))
        raw_transaction_menu.addAction(_("From &file"), self.do_process_from_file)
        raw_transaction_menu.addAction(_("From &text"), self.do_process_from_text)
        raw_transaction_menu.addAction(_("From the &blockchain"), self.do_process_from_txid)
        raw_transaction_menu.addAction(_("From &QR code"), self.read_tx_from_qrcode)
        self.raw_transaction_menu = raw_transaction_menu
        run_hook('init_menubar_tools', self, tools_menu)

        help_menu = menubar.addMenu(_("&Help"))
        help_menu.addAction(_("&About"), self.show_about)
        help_menu.addAction(_("&Check for updates..."), lambda: self.gui_object.show_update_checker(self))
        help_menu.addAction(_("&Official website"), lambda: webopen("https://electroncash.org"))
        help_menu.addSeparator()
        help_menu.addAction(_("Documentation"), lambda: webopen("http://electroncash.readthedocs.io/")).setShortcut(QKeySequence.HelpContents)
        help_menu.addAction(_("&Report Bug"), self.show_report_bug)
        help_menu.addSeparator()
        help_menu.addAction(_("&Donate to server"), self.donate_to_server)

        self.setMenuBar(menubar)

    def donate_to_server(self):
        d = self.network.get_donation_address()
        if d:
            host = self.network.get_parameters()[0]
            # The message is intentionally untranslated, leave it like that
            self.pay_to_URI('{}:{}?message=donation for {}'
                            .format(networks.net.CASHADDR_PREFIX, d, host))
        else:
            self.show_error(_('No donation address for this server'))

    def show_about(self):
        QMessageBox.about(self, "Electron Cash",
            _("Version")+" %s" % (self.wallet.electrum_version) + "\n\n" +
                _("Electron Cash's focus is speed, with low resource usage and simplifying Bitcoin Cash. You do not need to perform regular backups, because your wallet can be recovered from a secret phrase that you can memorize or write on paper. Startup times are instant because it operates in conjunction with high-performance servers that handle the most complicated parts of the Bitcoin Cash system."  + "\n\n" +
                _("Uses icons from the Icons8 icon pack (icons8.com).")))

    def show_report_bug(self):
        msg = ' '.join([
            _("Please report any bugs as issues on github:<br/>"),
            "<a href=https://github.com/simpleledger/Electron-Cash-SLP/issues>https://github.com/simpleledger/Electron-Cash-SLP/issues</a><br/><br/>",
            _("Before reporting a bug, upgrade to the most recent version of Electron Cash (latest release or git HEAD), and include the version number in your report."),
            _("Try to explain not only what the bug is, but how it occurs.")
         ])
        self.show_message(msg, title="Electron Cash - " + _("Reporting Bugs"), rich_text = True)

    def notify(self, message):
        self.gui_object.notify(message)


    # custom wrappers for getOpenFileName and getSaveFileName, that remember the path selected by the user
    def getOpenFileName(self, title, filter = ""):
        return __class__.static_getOpenFileName(title=title, filter=filter, config=self.config, parent=self)

    def getSaveFileName(self, title, filename, filter = ""):
        return __class__.static_getSaveFileName(title=title, filename=filename, filter=filter, config=self.config, parent=self)

    @staticmethod
    def static_getOpenFileName(*, title, parent=None, config=None, filter=""):
        if not config:
            config = get_config()
        userdir = os.path.expanduser('~')
        directory = config.get('io_dir', userdir) if config else userdir
        fileName, __ = QFileDialog.getOpenFileName(parent, title, directory, filter)
        if fileName and directory != os.path.dirname(fileName) and config:
            config.set_key('io_dir', os.path.dirname(fileName), True)
        return fileName

    @staticmethod
    def static_getSaveFileName(*, title, filename, parent=None, config=None, filter=""):
        if not config:
            config = get_config()
        userdir = os.path.expanduser('~')
        directory = config.get('io_dir', userdir) if config else userdir
        path = os.path.join( directory, filename )
        fileName, __ = QFileDialog.getSaveFileName(parent, title, path, filter)
        if fileName and directory != os.path.dirname(fileName) and config:
            config.set_key('io_dir', os.path.dirname(fileName), True)
        return fileName

    def timer_actions(self):

        # Note this runs in the GUI thread

        if self.need_update.is_set():
            self._update_wallet() # will clear flag when it runs. (also clears labels_need_update as well)

        if self.labels_need_update.is_set():
            self._update_labels() # will clear flag when it runs.

        # resolve aliases
        # FIXME this is a blocking network call that has a timeout of 5 sec
        self.payto_e.resolve()
        # update fee
        if self.require_fee_update:
            self.do_update_fee()
            self.require_fee_update = False

        # hook for other classes to be called here. For example the tx_update_mgr is called here (see TxUpdateMgr.do_check).
        self.on_timer_signal.emit()

    def format_amount(self, x, is_diff=False, whitespaces=False):
        return format_satoshis(x, self.num_zeros, self.decimal_point, is_diff=is_diff, whitespaces=whitespaces)

    def format_amount_and_units(self, amount, is_diff=False):
        text = self.format_amount(amount, is_diff=is_diff) + ' '+ self.base_unit()
        x = self.fx.format_amount_and_units(amount, is_diff=is_diff)
        if text and x:
            text += ' (%s)'%x
        return text

    def format_fee_rate(self, fee_rate):
        return format_fee_satoshis(fee_rate/1000, max(self.num_zeros, 1)) + ' sat/byte'

    def get_decimal_point(self):
        return self.decimal_point

    def base_unit(self):
        if self.decimal_point in util.inv_base_units:
            return util.inv_base_units[self.decimal_point]
        raise Exception('Unknown base unit')

    def connect_fields(self, window, btc_e, fiat_e, fee_e):

        def edit_changed(edit):
            if edit.follows:
                return
            edit.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
            fiat_e.is_last_edited = (edit == fiat_e)
            amount = edit.get_amount()
            rate = self.fx.exchange_rate() if self.fx else None
            if rate is None or amount is None:
                if edit is fiat_e:
                    btc_e.setText("")
                    if fee_e:
                        fee_e.setText("")
                else:
                    fiat_e.setText("")
            else:
                if edit is fiat_e:
                    btc_e.follows = True
                    btc_e.setAmount(int(amount / PyDecimal(rate) * COIN))
                    btc_e.setStyleSheet(ColorScheme.BLUE.as_stylesheet())
                    btc_e.follows = False
                    if fee_e:
                        window.update_fee()
                else:
                    fiat_e.follows = True
                    fiat_e.setText(self.fx.ccy_amount_str(
                        amount * PyDecimal(rate) / COIN, False))
                    fiat_e.setStyleSheet(ColorScheme.BLUE.as_stylesheet())
                    fiat_e.follows = False

        btc_e.follows = False
        fiat_e.follows = False
        fiat_e.textChanged.connect(partial(edit_changed, fiat_e))
        btc_e.textChanged.connect(partial(edit_changed, btc_e))
        fiat_e.is_last_edited = False

    _network_status_tip_dict = dict()
    def update_status(self):
        if not self.wallet:
            return

        icon_dict = ElectrumWindow.status_icon_dict
        if not icon_dict:
            # cache the icons to save on CPU overhead per update_status call
            icon_dict.update({
                "status_disconnected"         : QIcon(":icons/status_disconnected.svg"),
                "status_waiting"              : QIcon(":icons/status_waiting.svg"),
                "status_lagging"              : QIcon(":icons/status_lagging.svg"),
                "status_lagging_fork"         : QIcon(":icons/status_lagging_fork.svg"),
                "status_connected"            : QIcon(":icons/status_connected.svg"),
                "status_connected_fork"       : QIcon(":icons/status_connected_fork.svg"),
                "status_connected_proxy"      : QIcon(":icons/status_connected_proxy.svg"),
                "status_connected_proxy_fork" : QIcon(":icons/status_connected_proxy_fork.svg"),
                "seed_ok" : QIcon(":icons/seed.png"),
                "seed_warning" : QIcon(":icons/seed_warning.png")
            })
        status_tip_dict = ElectrumWindow._network_status_tip_dict
        if not status_tip_dict:
            # Since we're caching stuff, might as well cache this too
            status_tip_dict.update({
                "status_disconnected"         : _('Network Status') + " - " + _("Offline"),
                "status_waiting"              : _('Network Status') + " - " + _("Updating..."),
                "status_lagging"              : _('Network Status') + " - " + '',
                "status_lagging_fork"         : _('Network Status') + " - " + _("Chain fork(s) detected"),
                "status_connected"            : _('Network Status') + " - " + _("Connected"),
                "status_connected_fork"       : _('Network Status') + " - " + _("Chain fork(s) detected"),
                "status_connected_proxy"      : _('Network Status') + " - " + _("Connected via proxy"),
                "status_connected_proxy_fork" : _('Network Status') + " - " + _("Connected via proxy") + "; " + _("Chain fork(s) detected"),
            })


        status_tip = ''
        if self.network is None or not self.network.is_running():
            text = _("Offline")
            icon = icon_dict["status_disconnected"]
            status_tip = status_tip_dict['status_disconnected']

        elif self.network.is_connected():
            server_height = self.network.get_server_height()
            server_lag = self.network.get_local_height() - server_height
            num_chains = len(self.network.get_blockchains())
            # Server height can be 0 after switching to a new server
            # until we get a headers subscription request response.
            # Display the synchronizing message in that case.
            if not self.wallet.up_to_date or server_height == 0:
                text = _("Synchronizing...")
                icon = icon_dict["status_waiting"]
                status_tip = status_tip_dict["status_waiting"]
            elif server_lag > 1:
                text = _("Server is lagging ({} blocks)").format(server_lag)
                if num_chains <= 1:
                    icon = icon_dict["status_lagging"]
                    status_tip = status_tip_dict["status_lagging"] + text
                else:
                    icon = icon_dict["status_lagging_fork"]
                    status_tip = status_tip_dict["status_lagging_fork"] + "; " + text
            else:
                text = ""
                if not self.is_slp_wallet:
                    text += "Tokens Disabled - "
                token_id = self.slp_token_id
                try:
                    d = self.wallet.token_types[token_id]
                except (AttributeError, KeyError):
                    pass
                else:
                    bal = format_satoshis_nofloat(self.wallet.get_slp_token_balance(token_id, { 'user_config': { 'confirmed_only': False } })[0],
                                                  decimal_point=d['decimals'],)
                    text += "%s Token Balance: %s; "%(d['name'], bal)
                c, u, x = self.wallet.get_balance()
                text +=  _("BCH Balance" ) + ": %s "%(self.format_amount_and_units(c))
                if u:
                    text +=  " [%s unconfirmed]"%(self.format_amount(u, True).strip())
                if x:
                    text +=  " [%s unmatured]"%(self.format_amount(x, True).strip())

                # append fiat balance and price
                if self.fx.is_enabled():
                    text += self.fx.get_fiat_status_text(c + u + x,
                        self.base_unit(), self.get_decimal_point()) or ''
                n_unverif = self.wallet.get_unverified_tx_pending_count()
                if n_unverif >= 10:
                    # if there are lots left to verify, display this informative text
                    text += " " + ( _("[%d unverified TXs]") % n_unverif )
                if not self.network.proxy:
                    icon = icon_dict["status_connected"] if num_chains <= 1 else icon_dict["status_connected_fork"]
                    status_tip = status_tip_dict["status_connected"] if num_chains <= 1 else status_tip_dict["status_connected_fork"]
                else:
                    icon = icon_dict["status_connected_proxy"] if num_chains <= 1 else icon_dict["status_connected_proxy_fork"]
                    status_tip = status_tip_dict["status_connected_proxy"] if num_chains <= 1 else status_tip_dict["status_connected_proxy_fork"]

                # Provide extra warning and instructions to user if he/she has tokens in a non-SLP wallet type.
                if not self.is_slp_wallet:
                    locked_in_slp = self.wallet.get_slp_locked_balance()
                    if locked_in_slp > 0:
                        self._warn_slp_prefers_slp_wallets_if_not_slp_wallet()
        else:
            text = _("Not connected")
            icon = icon_dict["status_disconnected"]
            status_tip = status_tip_dict["status_disconnected"]

        self.tray.setToolTip("%s (%s)" % (text, self.wallet.basename()))
        self.balance_label.setText(text)
        addr_format = self.config.get('addr_format', 1)
        self.setAddrFormatText(addr_format)
        self.status_button.setIcon( icon )
        self.status_button.setStatusTip( status_tip )
        if self.wallet.has_seed():
            if self.wallet.storage.get('wallet_seed_needs_backup'):
                self.seed_button.setIcon(icon_dict["seed_warning"])
                self.seed_button.setToolTip(_("Seed Requires Backup!"))
                self.seed_button.setStatusTip(self.seed_button.toolTip())
            else:
                self.seed_button.setIcon(icon_dict["seed_ok"])
                self.seed_button.setToolTip(_("Seed"))
                self.seed_button.setStatusTip(None)

    def update_wallet(self):
        self.need_update.set() # will enqueue an _update_wallet() call in at most 0.5 seconds from now.

    def _update_wallet(self):
        ''' Called by self.timer_actions every 0.5 secs if need_update flag is set.
            Note that the flag is actually cleared by update_tabs.'''
        self.update_status()
        if self.wallet.up_to_date or not self.network or not self.network.is_connected():
            self.update_tabs()

    @rate_limited(1.0, classlevel=True, ts_after=True) # Limit tab updates to no more than 1 per second, app-wide. Multiple calls across instances will be collated into 1 deferred series of calls (1 call per extant instance)
    def update_tabs(self):
        if self.cleaned_up: return
        self.history_list.update()
        self.request_list.update()
        self.address_list.update()
        self.utxo_list.update()
        self.contact_list.update()
        self.invoice_list.update()
        self.update_completions()
        if self.is_slp_wallet:
            self.slp_history_list.update()
            self.token_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history, also clears self.tx_update_mgr.verif_q
        self.need_update.clear() # clear flag
        if self.labels_need_update.is_set():
            # if flag was set, might as well declare the labels updated since they necessarily were due to a full update.
            self.labels_updated_signal.emit() # just in case client code was waiting for this signal to proceed.
            self.labels_need_update.clear() # clear flag

    def update_labels(self):
        self.labels_need_update.set() # will enqueue an _update_labels() call in at most 0.5 seconds from now

    @rate_limited(1.0)
    def _update_labels(self):
        ''' Called by self.timer_actions every 0.5 secs if labels_need_update flag is set. '''
        if self.cleaned_up: return
        self.history_list.update_labels()
        self.address_list.update_labels()
        self.utxo_list.update_labels()
        self.update_completions()
        self.labels_updated_signal.emit()
        self.labels_need_update.clear() # clear flag

    def create_history_tab(self):
        from .history_list import HistoryList
        self.history_list = l = HistoryList(self)
        l.searchable_list = l
        return l

    def create_slp_history_tab(self):
        from .slp_history_list import HistoryList
        self.slp_history_list = l = HistoryList(self)
        return self.create_list_tab(l)

    def show_address(self, addr, *, parent=None):
        parent = parent or self
        from . import address_dialog
        d = address_dialog.AddressDialog(self,  addr, windowParent=parent)
        d.exec_()

    def show_transaction(self, tx, tx_desc = None):
        '''tx_desc is set only for txs created in the Send tab'''
        d = show_transaction(tx, self, tx_desc)
        self._tx_dialogs.add(d)

    def addr_toggle_slp(self, force_slp=False):

        def present_slp():
            self.toggle_cashaddr(2, True)
            self.receive_slp_token_type_label.setDisabled(False)
            self.receive_slp_amount_e.setDisabled(False)
            self.receive_slp_amount_label.setDisabled(False)

        if force_slp:
            present_slp()
            return

        if Address.FMT_UI == Address.FMT_SLPADDR:
            self.toggle_cashaddr(1, True)
            self.receive_token_type_combo.setCurrentIndex(0)
        else:
            present_slp()

    def on_toggled_opreturn(self, b):
        ''' toggles opreturn-related widgets for both the receive and send
        tabs'''
        b = bool(b)
        self.config.set_key('enable_opreturn', b)
        # send tab
        if not b:
            self.message_opreturn_e.setText("")
            self.op_return_toolong = False
        for x in self.send_tab_opreturn_widgets:
            x.setVisible(b)
        # receive tab
        for x in self.receive_tab_opreturn_widgets:
            x.setVisible(b)

    def create_receive_tab(self):
        # A 4-column grid layout.  All the stretch is in the last column.
        # The exchange rate plugin adds a fiat widget in column 2
        self.receive_grid = grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(3, 1)

        self.receive_address = None
        self.receive_address_e = ButtonsLineEdit()
        self.receive_address_e.addCopyButton()
        self.receive_address_e.setReadOnly(True)
        msg = _('Bitcoin Cash address where the payment should be received. Note that each payment request uses a different Bitcoin Cash address.')
        label = HelpLabel(_('&Receiving address'), msg)
        label.setBuddy(self.receive_address_e)
        self.receive_address_e.textChanged.connect(self.update_receive_qr)
        self.cashaddr_toggled_signal.connect(self.update_receive_address_widget)
        grid.addWidget(label, 0, 0)
        grid.addWidget(self.receive_address_e, 0, 1, 1, -1)

        if self.is_slp_wallet:
            self.show_slp_addr_btn = QPushButton(_('Show Token Address'))
            self.show_slp_addr_btn.clicked.connect(self.addr_toggle_slp)
            grid.addWidget(self.show_slp_addr_btn, 1, 1)

        self.receive_message_e = QLineEdit()
        label = QLabel(_('&Description'))
        label.setBuddy(self.receive_message_e)
        grid.addWidget(label, 2, 0)
        grid.addWidget(self.receive_message_e, 2, 1, 1, -1)
        self.receive_message_e.textChanged.connect(self.update_receive_qr)

        # OP_RETURN requests
        self.receive_opreturn_e = QLineEdit()
        msg = _("You may optionally append an OP_RETURN message to the payment URI and/or QR you generate.\n\nNote: Not all wallets yet support OP_RETURN parameters, so make sure the other party's wallet supports OP_RETURN URIs.")
        self.receive_opreturn_label = label = HelpLabel(_('&OP_RETURN'), msg)
        label.setBuddy(self.receive_opreturn_e)
        self.receive_opreturn_rawhex_cb = QCheckBox(_('Raw &hex script'))
        self.receive_opreturn_rawhex_cb.setToolTip(_('If unchecked, the textbox contents are UTF8-encoded into a single-push script: <tt>OP_RETURN PUSH &lt;text&gt;</tt>. If checked, the text contents will be interpreted as a raw hexadecimal script to be appended after the OP_RETURN opcode: <tt>OP_RETURN &lt;script&gt;</tt>.'))
        grid.addWidget(label, 3, 0)
        grid.addWidget(self.receive_opreturn_e, 3, 1, 1, 3)
        grid.addWidget(self.receive_opreturn_rawhex_cb, 3, 4, Qt.AlignLeft)
        self.receive_opreturn_e.textChanged.connect(self.update_receive_qr)
        self.receive_opreturn_rawhex_cb.clicked.connect(self.update_receive_qr)
        self.receive_tab_opreturn_widgets = [
            self.receive_opreturn_e,
            self.receive_opreturn_rawhex_cb,
            self.receive_opreturn_label,
        ]

        msg = _('Select the SLP token to Request.')
        self.receive_token_type_combo = QComboBox()
        if ColorScheme.dark_scheme and sys.platform == 'darwin':
            # Hack/Workaround to QDarkStyle bugs; see https://github.com/ColinDuquesnoy/QDarkStyleSheet/issues/169#issuecomment-494647801
            self.receive_token_type_combo.setItemDelegate(QStyledItemDelegate(self.receive_token_type_combo))
        self.receive_token_type_combo.setFixedWidth(200)
        self.receive_token_type_combo.currentIndexChanged.connect(self.on_slptok_receive)
        #self.receive_token_type_combo.currentIndexChanged.connect(self.update_buttons_on_seed)  # update 'CoinText' button, etc
        self.receive_slp_token_type_label = HelpLabel(_('Token Type'), msg)
        grid.addWidget(self.receive_slp_token_type_label, 4, 0)
        grid.addWidget(self.receive_token_type_combo, 4, 1)

        self.receive_slp_amount_e = SLPAmountEdit('tokens', 0)
        self.receive_slp_amount_e.setFixedWidth(self.receive_token_type_combo.width())
        self.receive_slp_amount_label = QLabel(_('Req. token amount'))
        grid.addWidget(self.receive_slp_amount_label, 5, 0)
        grid.addWidget(self.receive_slp_amount_e, 5, 1)
        self.receive_slp_amount_e.textChanged.connect(self.update_receive_qr)

        self.receive_amount_e = BTCAmountEdit(self.get_decimal_point)
        self.receive_amount_e.setFixedWidth(self.receive_token_type_combo.width())
        self.receive_amount_label = QLabel(_('Requested &amount'))
        self.receive_amount_label.setBuddy(self.receive_amount_e)
        grid.addWidget(self.receive_amount_label, 6, 0)
        grid.addWidget(self.receive_amount_e, 6, 1)
        self.receive_amount_e.textChanged.connect(self.update_receive_qr)

        if Address.FMT_UI != Address.FMT_SLPADDR:
            self.receive_token_type_combo.setDisabled(True)
            self.receive_slp_token_type_label.setDisabled(True)
            self.receive_slp_amount_e.setDisabled(True)
            self.receive_slp_amount_label.setDisabled(True)
        else:
            self.receive_token_type_combo.setDisabled(False)
            self.receive_slp_token_type_label.setDisabled(False)
            self.receive_slp_amount_e.setDisabled(False)
            self.receive_slp_amount_label.setDisabled(False)

        self.fiat_receive_e = AmountEdit(self.fx.get_currency if self.fx else '')
        if not self.fx or not self.fx.is_enabled():
            self.fiat_receive_e.setVisible(False)
        grid.addWidget(self.fiat_receive_e, 6, 2, Qt.AlignLeft)
        self.connect_fields(self, self.receive_amount_e, self.fiat_receive_e, None)

        self.expires_combo = QComboBox()
        self.expires_combo.addItems([i[0] for i in expiration_values])
        self.expires_combo.setCurrentIndex(3)
        self.expires_combo.setFixedWidth(self.receive_amount_e.width())
        msg = ' '.join([
            _('Expiration date of your request.'),
            _('This information is seen by the recipient if you send them a signed payment request.'),
            _('Expired requests have to be deleted manually from your list, in order to free the corresponding Bitcoin Cash addresses.'),
            _('The Bitcoin Cash address never expires and will always be part of this Electron Cash wallet.'),
        ])
        label = HelpLabel(_('Request &expires'), msg)
        label.setBuddy(self.expires_combo)
        grid.addWidget(label, 7, 0)
        grid.addWidget(self.expires_combo, 7, 1)
        self.expires_label = QLineEdit('')
        self.expires_label.setReadOnly(1)
        self.expires_label.hide()
        grid.addWidget(self.expires_label, 7, 1)

        self.save_request_button = QPushButton(_('&Save'))
        self.save_request_button.clicked.connect(self.save_payment_request)

        self.new_request_button = QPushButton(_('&Clear'))
        self.new_request_button.clicked.connect(self.new_payment_request)

        weakSelf = Weak.ref(self)

        class MyQRCodeWidget(QRCodeWidget):
            def mouseReleaseEvent(self, e):
                ''' to make the QRWidget clickable '''
                weakSelf() and weakSelf().show_qr_window()

        self.receive_qr = MyQRCodeWidget(fixedSize=200)
        self.receive_qr.setCursor(QCursor(Qt.PointingHandCursor))

        self.receive_buttons = buttons = QHBoxLayout()
        buttons.addWidget(self.save_request_button)
        buttons.addWidget(self.new_request_button)
        buttons.addStretch(1)
        grid.addLayout(buttons, 8, 1, 1, -1)

        self.receive_requests_label = QLabel(_('Re&quests'))

        from .request_list import RequestList
        self.request_list = RequestList(self)
        self.request_list.chkVisible()

        self.receive_requests_label.setBuddy(self.request_list)

        # layout
        vbox_g = QVBoxLayout()
        vbox_g.addLayout(grid)
        vbox_g.addStretch()

        hbox = QHBoxLayout()
        hbox.addLayout(vbox_g)
        vbox2 = QVBoxLayout()
        vbox2.setContentsMargins(0,0,0,0)
        vbox2.setSpacing(4)
        vbox2.addWidget(self.receive_qr, Qt.AlignHCenter|Qt.AlignTop)
        self.receive_qr.setToolTip(_('Receive request QR code (click for details)'))
        but = uribut = QPushButton(_('Copy &URI'))
        def on_copy_uri():
            if self.receive_qr.data:
                uri = str(self.receive_qr.data)
                self.copy_to_clipboard(uri, _('Receive request URI copied to clipboard'), uribut)
        but.clicked.connect(on_copy_uri)
        but.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        but.setToolTip(_('Click to copy the receive request URI to the clipboard'))
        vbox2.addWidget(but)
        vbox2.setAlignment(but, Qt.AlignHCenter|Qt.AlignVCenter)

        hbox.addLayout(vbox2)

        class ReceiveTab(QWidget):
            def showEvent(self, e):
                super().showEvent(e)
                if e.isAccepted():
                    slf = weakSelf()
                    if slf:
                        slf.check_and_reset_receive_address_if_needed()
                if self.main_window.is_slp_wallet:
                    c, u, x = self.main_window.wallet.get_balance()
                    bal = c + u - self.main_window.wallet.get_slp_locked_balance()
                    if bal < 1000:
#                       if not self.low_balance_warning_shown:
#                           self.main_window.show_warning("Low BCH balance.\n\nCreating and sending SLP tokens requires Bitcoin Cash to cover transaction fees.  We recommend a minimum of 0.0001 BCH to get started.\n\nSend BCH to the address displayed in the 'Receive' tab.")
                        self.main_window.toggle_cashaddr(1, True)
                        self.low_balance_warning_shown = False
                    else:
                        self.main_window.toggle_cashaddr(2, True)
                    if Address.FMT_UI == Address.FMT_SLPADDR:
                        self.main_window.show_slp_addr_btn.setText("Show BCH Address")
                    else:
                        self.main_window.show_slp_addr_btn.setText("Show Token Address")
                else:
                    self.main_window.toggle_cashaddr(1, True)


        w = ReceiveTab()
        w.low_balance_warning_shown = False
        w.main_window = self
        w.searchable_list = self.request_list
        vbox = QVBoxLayout(w)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        vbox.addWidget(self.receive_requests_label)
        vbox.addWidget(self.request_list)
        vbox.setStretchFactor(self.request_list, 1000)

        return w


    def delete_payment_request(self, addr):
        self.wallet.remove_payment_request(addr, self.config)
        self.request_list.update()
        self.address_list.update()
        self.clear_receive_tab()

    def get_request_URI(self, addr):
        req = self.wallet.receive_requests[addr]
        message = self.wallet.labels.get(addr.to_storage_string(), '')
        amount = req['amount']
        op_return = req.get('op_return')
        op_return_raw = req.get('op_return_raw') if not op_return else None
        URI = web.create_URI(addr, amount, message, op_return=op_return, op_return_raw=op_return_raw)
        if req.get('time'):
            URI += "&time=%d"%req.get('time')
        if req.get('exp'):
            URI += "&exp=%d"%req.get('exp')
        if req.get('name') and req.get('sig'):
            sig = bfh(req.get('sig'))
            sig = bitcoin.base_encode(sig, base=58)
            URI += "&name=" + req['name'] + "&sig="+sig
        return str(URI)


    def sign_payment_request(self, addr):
        alias = self.config.get('alias')
        alias_privkey = None
        if alias and self.alias_info:
            alias_addr, alias_name, validated = self.alias_info
            if alias_addr:
                if self.wallet.is_mine(alias_addr):
                    msg = _('This payment request will be signed.') + '\n' + _('Please enter your password')
                    password = self.password_dialog(msg)
                    if password:
                        try:
                            self.wallet.sign_payment_request(addr, alias, alias_addr, password)
                        except Exception as e:
                            self.show_error(str(e))
                            return
                    else:
                        return
                else:
                    return

    def save_payment_request(self):
        if not self.receive_address:
            self.show_error(_('No receiving address'))
        if self.receive_token_type_combo.currentData() is not None:
            amount = float(self.receive_slp_amount_e.text())
        else:
            amount = self.receive_amount_e.get_amount()
        message = self.receive_message_e.text()
        if not message and not amount:
            self.show_error(_('No message or amount'))
            return False
        i = self.expires_combo.currentIndex()
        expiration = list(map(lambda x: x[1], expiration_values))[i]
        kwargs = {}
        opr = self.receive_opreturn_e.text().strip()
        if opr:
            # save op_return, if any
            arg = 'op_return'
            if self.receive_opreturn_rawhex_cb.isChecked():
                arg = 'op_return_raw'
            kwargs[arg] = opr
        if self.receive_token_type_combo.currentData() is not None:
            tokenid = self.receive_token_type_combo.currentData()
            req = self.wallet.make_payment_request(self.receive_address, amount,
                                    message, expiration, token_id=tokenid, **kwargs)
        else: 
            req = self.wallet.make_payment_request(self.receive_address, amount,
                                                message, expiration, **kwargs)
        self.wallet.add_payment_request(req, self.config)
        self.sign_payment_request(self.receive_address)
        self.request_list.update()
        self.request_list.select_item_by_address(req.get('address'))  # when adding items to the view the current selection may not reflect what's in the UI. Make sure it's selected.
        self.address_list.update()
        self.save_request_button.setEnabled(False)

    def view_and_paste(self, title, msg, data):
        dialog = WindowModalDialog(self.top_level_window(), title)
        vbox = QVBoxLayout()
        label = QLabel(msg)
        label.setWordWrap(True)
        vbox.addWidget(label)
        pr_e = ShowQRTextEdit(text=data)
        vbox.addWidget(pr_e)
        vbox.addLayout(Buttons(CopyCloseButton(pr_e.text, self.app, dialog)))
        dialog.setLayout(vbox)
        dialog.exec_()

    def export_payment_request(self, addr):
        r = self.wallet.receive_requests[addr]
        pr = paymentrequest.serialize_request(r).SerializeToString()
        name = r['id'] + '.bip70'
        fileName = self.getSaveFileName(_("Select where to save your payment request"), name, "*.bip70")
        if fileName:
            with open(fileName, "wb+") as f:
                f.write(util.to_bytes(pr))
            self.show_message(_("Request saved successfully"))
            self.saved = True

    def new_payment_request(self):
        self.receive_token_type_combo.setCurrentIndex(0)
        self.receive_slp_amount_e.setText("")
        addr = self.wallet.get_unused_address(frozen_ok=False)
        if addr is None:
            if not self.wallet.is_deterministic():
                msg = [
                    _('No more addresses in your wallet.'),
                    _('You are using a non-deterministic wallet, which cannot create new addresses.'),
                    _('If you want to create new addresses, use a deterministic wallet instead.')
                   ]
                self.show_message(' '.join(msg))
                # New! Since the button is called 'Clear' now, we let them proceed with a re-used address
                addr = self.wallet.get_receiving_address()
            else:
                # Warn if past gap limit.
                if not self.question(_("Warning: The next address will not be recovered automatically if you restore your wallet from seed; you may need to add it manually.\n\nThis occurs because you have too many unused addresses in your wallet. To avoid this situation, use the existing addresses first.\n\nCreate anyway?")):
                    return
                addr = self.wallet.create_new_address(False)
        self.set_receive_address(addr)
        self.expires_label.hide()
        self.expires_combo.show()
        self.request_list.setCurrentItem(None)  # We want the current item to always reflect what's in the UI. So if new, clear selection.
        self.receive_message_e.setFocus(1)

    def set_receive_address(self, addr):
        self.receive_address = addr
        self.receive_message_e.setText('')
        self.receive_opreturn_rawhex_cb.setChecked(False)
        self.receive_opreturn_e.setText('')
        self.receive_amount_e.setAmount(None)
        self.update_receive_address_widget()

    def update_receive_address_widget(self):
        text = ''
        if self.receive_address:
            text = self.receive_address.to_full_ui_string()
        self.receive_address_e.setText(text)

    @rate_limited(0.250, ts_after=True)  # this function potentially re-computes the QR widget, so it's rate limited to once every 250ms
    def check_and_reset_receive_address_if_needed(self):
        ''' Check to make sure the receive tab is kosher and doesn't contain
        an already-used address. This should be called from the showEvent
        for the tab. '''
        if not self.wallet.use_change or self.cleaned_up:
            # if they don't care about change addresses, they are ok
            # with re-using addresses, so skip this check.
            return
        # ok, they care about anonymity, so make sure the receive address
        # is always an unused address.
        if (not self.receive_address  # this should always be defined but check anyway
            or self.receive_address in self.wallet.frozen_addresses  # make sure it's not frozen
            or (self.wallet.get_address_history(self.receive_address)   # make a new address if it has a history
                and not self.wallet.get_payment_request(self.receive_address, self.config))):  # and if they aren't actively editing one in the request_list widget
            addr = self.wallet.get_unused_address(frozen_ok=False)  # try unused, not frozen
            if addr is None:
                if self.wallet.is_deterministic():
                    # creae a new one if deterministic
                    addr = self.wallet.create_new_address(False)
                else:
                    # otherwise give up and just re-use one.
                    addr = self.wallet.get_receiving_address()
            self.receive_address = addr
            self.update_receive_address_widget()

    def clear_receive_tab(self):
        self.expires_label.hide()
        self.expires_combo.show()
        self.request_list.setCurrentItem(None)
        self.set_receive_address(self.wallet.get_receiving_address(frozen_ok=False))

    def show_qr_window(self):
        from . import qrwindow
        if not self.qr_window:
            self.qr_window = qrwindow.QR_Window()
            self.qr_window.setAttribute(Qt.WA_DeleteOnClose, True)
            weakSelf = Weak.ref(self)
            def destroyed_clean(x):
                if weakSelf():
                    weakSelf().qr_window = None
                    weakSelf().print_error("QR Window destroyed.")
            self.qr_window.destroyed.connect(destroyed_clean)
        self.update_receive_qr()
        if self.qr_window.isMinimized():
            self.qr_window.showNormal()
        else:
            self.qr_window.show()
        self.qr_window.raise_()
        self.qr_window.activateWindow()

    def show_send_tab(self):
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.send_tab))

    def show_receive_tab(self):
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.receive_tab))

    def receive_at(self, addr):
        self.receive_address = addr
        self.show_receive_tab()
        self.update_receive_address_widget()

    def update_receive_qr(self):
        if self.receive_token_type_combo.currentData() is not None:
            amount = self.receive_slp_amount_e.text() if self.receive_slp_amount_e.text() is not '' else None
            token_id = self.receive_token_type_combo.currentData()
        else:
            amount = self.receive_amount_e.get_amount()
            token_id = None
        message = self.receive_message_e.text()
        self.save_request_button.setEnabled((amount is not None) or (message != ""))
        kwargs = {}
        if self.receive_opreturn_e.isVisible():
            # set op_return if enabled
            arg = 'op_return'
            if self.receive_opreturn_rawhex_cb.isChecked():
                arg = 'op_return_raw'
            opret = self.receive_opreturn_e.text()
            if opret:
                kwargs[arg] = opret

        # Special case hack -- see #1473. Omit bitcoincash: prefix from
        # legacy address if no other params present in receive request.
        if Address.FMT_UI == Address.FMT_LEGACY and not kwargs and not amount and not message:
            uri = self.receive_address.to_ui_string()
        elif not token_id:
            # Otherwise proceed as normal, prepending bitcoincash: to URI
            uri = web.create_URI(self.receive_address, amount, message, **kwargs)
        else: 
            uri = web.create_URI(self.receive_address, amount, message, **kwargs, token_id=token_id)

        self.receive_qr.setData(uri)
        if self.qr_window:
            self.qr_window.set_content(self, self.receive_address_e.text(), amount,
                                       message, uri, **kwargs)
        if self.is_slp_wallet:
            if Address.FMT_UI == Address.FMT_SLPADDR:
                self.show_slp_addr_btn.setText("Show BCH Address")
            else:
                self.show_slp_addr_btn.setText("Show Token Address")

    def on_slptok(self):
        self.slp_token_id = self.token_type_combo.currentData()
        self.payto_e.check_text()
        self.slp_amount_e.setText("")
        if self.slp_token_id is None:
            self.amount_e.setDisabled(False)
            self.amount_label.setDisabled(False)
            self.max_button.setDisabled(False)
            self.fiat_send_e.setDisabled(False)
            self.slp_extra_bch_cb.setHidden(True)
            self.slp_amount_e.setDisabled(True)
            self.slp_max_button.setDisabled(True)
            self.slp_amount_label.setDisabled(True)
            self.message_opreturn_e.setEnabled(True)
            self.opreturn_rawhex_cb.setEnabled(True)
            self.opreturn_label.setEnabled(True)
        else:
            self.slp_extra_bch_cb.setHidden(False)
            self.slp_extra_bch_cb.setChecked(False)
            self.slp_extra_bch_cb.clicked.emit()
            self.slp_amount_e.setDisabled(False)
            self.slp_max_button.setDisabled(False)
            self.slp_amount_label.setDisabled(False)
            tok = self.wallet.token_types[self.slp_token_id]
            self.slp_amount_e.set_token(tok['name'][:6],tok['decimals'])
            self.message_opreturn_e.setEnabled(False)
            self.message_opreturn_e.setText('')
            self.opreturn_rawhex_cb.setEnabled(False)
            self.opreturn_label.setEnabled(False)
        self.update_status()
        self.do_update_fee()

    def on_slptok_receive(self):
        self.receive_slp_amount_e.setText("")
        self.receive_amount_e.setText("")
        slp_token_id = self.receive_token_type_combo.currentData()
        if slp_token_id is None:
            self.receive_slp_amount_e.setDisabled(True)
            self.receive_slp_amount_label.setDisabled(True)
            self.receive_amount_e.setDisabled(False)
            self.receive_amount_label.setDisabled(False)
            self.fiat_receive_e.setDisabled(False)
        else:
            self.addr_toggle_slp(True)
            self.receive_slp_amount_e.setDisabled(False)
            self.receive_slp_amount_label.setDisabled(False)
            self.receive_amount_e.setDisabled(True)
            self.receive_amount_label.setDisabled(True)
            self.fiat_receive_e.setDisabled(True)
            tok = self.wallet.token_types[slp_token_id]
            self.receive_slp_amount_e.set_token(tok['name'][:6],tok['decimals'])

    def on_slp_extra_bch(self):
        if self.slp_extra_bch_cb.isChecked():
            self.amount_e.setDisabled(False)
            self.amount_label.setDisabled(False)
            self.max_button.setDisabled(False)
            self.fiat_send_e.setDisabled(False)
        else:
            self.amount_e.setText('')
            self.max_button.setChecked(False)
            self.amount_e.setDisabled(True)
            self.amount_label.setDisabled(True)
            self.max_button.setDisabled(True)
            self.fiat_send_e.setDisabled(True)

    def create_send_tab(self):
        # A 4-column grid layout.  All the stretch is in the last column.
        # The exchange rate plugin adds a fiat widget in column 2
        self.send_grid = grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(3, 1)

        from .paytoedit import PayToEdit
        self.amount_e = BTCAmountEdit(self.get_decimal_point)
        self.payto_e = PayToEdit(self)
        self.payto_e.parent=self

        self.slp_send_tab_widgets = []
        if self.is_slp_wallet:
            self.slp_amount_e = SLPAmountEdit('tokens', 0)
            self.token_type_combo = QComboBox()
            if ColorScheme.dark_scheme and sys.platform == 'darwin':
                # Hack/Workaround to QDarkStyle bugs; see https://github.com/ColinDuquesnoy/QDarkStyleSheet/issues/169#issuecomment-494647801
                self.token_type_combo.setItemDelegate(QStyledItemDelegate(self.token_type_combo))
            self.token_type_combo.setFixedWidth(200)
            self.token_type_combo.currentIndexChanged.connect(self.on_slptok)
            self.token_type_combo.currentIndexChanged.connect(self.update_buttons_on_seed)  # update 'CoinText' button, etc
            self.slp_send_tab_widgets += [
                self.slp_amount_e, self.token_type_combo
            ]

        msg = _('Recipient of the funds.') + '\n\n'\
              + _('You may enter a Bitcoin Cash address, a label from your list of contacts (a list of completions will be proposed), or an alias (email-like address that forwards to a Bitcoin Cash address)') + ".\n\n" \
              + _('You may also enter cointext:(NUMBER) to send a CoinText.')
        payto_label = HelpLabel(_('Pay &to'), msg)
        payto_label.setBuddy(self.payto_e)
        grid.addWidget(payto_label, 1, 0)
        grid.addWidget(self.payto_e, 1, 1, 1, -1)

        completer = QCompleter(self.payto_e)
        completer.setCaseSensitivity(False)
        self.payto_e.setCompleter(completer)
        completer.setModel(self.completions)

        msg = _('Description of the transaction (not mandatory).') + '\n\n'\
              + _('The description is not sent to the recipient of the funds. It is stored in your wallet file, and displayed in the \'History\' tab.')
        description_label = HelpLabel(_('&Description'), msg)
        grid.addWidget(description_label, 2, 0)
        self.message_e = MyLineEdit()
        description_label.setBuddy(self.message_e)
        grid.addWidget(self.message_e, 2, 1, 1, -1)

        msg_opreturn = ( _('OP_RETURN data (optional).') + '\n\n'
                        + _('Posts a PERMANENT note to the BCH blockchain as part of this transaction.')
                        + '\n\n' + _('If you specify OP_RETURN text, you may leave the \'Pay to\' field blank.') )
        self.opreturn_label = HelpLabel(_('&OP_RETURN'), msg_opreturn)
        grid.addWidget(self.opreturn_label,  3, 0)
        self.message_opreturn_e = MyLineEdit()
        self.opreturn_label.setBuddy(self.message_opreturn_e)
        hbox = QHBoxLayout()
        hbox.addWidget(self.message_opreturn_e)
        self.opreturn_rawhex_cb = QCheckBox(_('&Raw hex script'))
        self.opreturn_rawhex_cb.setToolTip(_('If unchecked, the textbox contents are UTF8-encoded into a single-push script: <tt>OP_RETURN PUSH &lt;text&gt;</tt>. If checked, the text contents will be interpreted as a raw hexadecimal script to be appended after the OP_RETURN opcode: <tt>OP_RETURN &lt;script&gt;</tt>.'))
        hbox.addWidget(self.opreturn_rawhex_cb)
        grid.addLayout(hbox,  3 , 1, 1, -1)

        self.send_tab_opreturn_widgets = [
            self.message_opreturn_e,
            self.opreturn_rawhex_cb,
            self.opreturn_label,
        ]

        self.from_label = QLabel(_('&From'))
        grid.addWidget(self.from_label, 4, 0)
        self.from_list = MyTreeWidget(self, self.from_list_menu, ['',''])
        self.from_label.setBuddy(self.from_list)
        self.from_list.setHeaderHidden(True)
        self.from_list.setMaximumHeight(80)
        grid.addWidget(self.from_list, 4, 1, 1, -1)
        self.set_pay_from([])

        if self.is_slp_wallet:
            msg = _('Token Amount to be sent.') + '\n\n' \
                + _("To enable make sure 'Address Mode' is set to SLP.") + '\n\n' \
                + _('The amount will be displayed in red if you do not have enough funds in your wallet.') + ' ' \
                + _('Note that if you have frozen some of your addresses, the available funds will be lower than your total balance.') + '\n\n' \
                + _('Keyboard shortcut: type "!" to send all your coins.')
            self.slp_amount_label = HelpLabel(_('Token Amount'), msg)

            msg = _('Select the SLP token to send.')
            self.slp_token_type_label = HelpLabel(_('Token Type'), msg)
            grid.addWidget(self.slp_token_type_label, 5, 0)
            grid.addWidget(self.token_type_combo, 5, 1)

            grid.addWidget(self.slp_amount_label, 6, 0)
            hbox = QHBoxLayout()
            self.amount_e.setMinimumWidth(195)
            self.slp_amount_e.setMinimumWidth(195)
            self.slp_amount_e.textEdited.connect(self.update_fee)
            hbox.addWidget(self.slp_amount_e)

            self.slp_max_button = EnterButton(_("Max"), self.slp_spend_max)
            hbox.addWidget(self.slp_max_button)
            grid.addLayout(hbox, 6, 1)

            self.slp_extra_bch_cb = QCheckBox(_('Also send BCH?'))
            self.slp_extra_bch_cb.clicked.connect(self.on_slp_extra_bch)
            self.slp_extra_bch_cb.setHidden(True)
            grid.addWidget(self.slp_extra_bch_cb, 6, 2)

            self.slp_send_tab_widgets += [
                self.slp_max_button, self.slp_extra_bch_cb
            ]

        msg = _('BCH amount to be sent.') + '\n\n' \
              + _('The amount will be displayed in red if you do not have enough funds in your wallet.') + ' ' \
              + _('Note that if you have frozen some of your addresses, the available funds will be lower than your total balance.') + '\n\n' \
              + _('Keyboard shortcut: type "!" to send all your coins.')
        self.amount_label = HelpLabel(_('BCH &Amount'), msg)
        self.amount_label.setBuddy(self.amount_e)
        grid.addWidget(self.amount_label, 7, 0)
        hbox = QHBoxLayout()
        hbox.addWidget(self.amount_e)

        self.max_button = EnterButton(_("&Max"), self.spend_max)
        self.max_button.setCheckable(True)
        hbox.addWidget(self.max_button)
        grid.addLayout(hbox, 7, 1)

        self.fiat_send_e = AmountEdit(self.fx.get_currency if self.fx else '')
        if not self.fx or not self.fx.is_enabled():
            self.fiat_send_e.setVisible(False)
        grid.addWidget(self.fiat_send_e, 7, 2)
        self.amount_e.frozen.connect(
            lambda: self.fiat_send_e.setFrozen(self.amount_e.isReadOnly()))

        msg = _('Bitcoin Cash transactions are in general not free. A transaction fee is paid by the sender of the funds.') + '\n\n'\
              + _('The amount of fee can be decided freely by the sender. However, transactions with low fees take more time to be processed.') + '\n\n'\
              + _('A suggested fee is automatically added to this field. You may override it. The suggested fee increases with the size of the transaction.')
        self.fee_e_label = HelpLabel(_('F&ee'), msg)

        def fee_cb(dyn, pos, fee_rate):
            if dyn:
                self.config.set_key('fee_level', pos, False)
            else:
                self.config.set_key('fee_per_kb', fee_rate, False)
            self.spend_max() if self.max_button.isChecked() else self.update_fee()

        self.fee_slider = FeeSlider(self, self.config, fee_cb)
        self.fee_e_label.setBuddy(self.fee_slider)
        self.fee_slider.setFixedWidth(140)

        self.fee_custom_lbl = HelpLabel(self.get_custom_fee_text(),
                                        _('This is the fee rate that will be used for this transaction.')
                                        + "\n\n" + _('It is calculated from the Custom Fee Rate in preferences, but can be overridden from the manual fee edit on this form (if enabled).')
                                        + "\n\n" + _('Generally, a fee of 1.0 sats/B is a good minimal rate to ensure your transaction will make it into the next block.'))
        self.fee_custom_lbl.setFixedWidth(140)

        self.fee_slider_mogrifier()

        self.fee_e = BTCAmountEdit(self.get_decimal_point)
        if not self.config.get('show_fee', False):
            self.fee_e.setVisible(False)
        self.fee_e.textEdited.connect(self.update_fee)
        # This is so that when the user blanks the fee and moves on,
        # we go back to auto-calculate mode and put a fee back.
        self.fee_e.editingFinished.connect(self.update_fee)
        self.connect_fields(self, self.amount_e, self.fiat_send_e, self.fee_e)

        grid.addWidget(self.fee_e_label, 9, 0)
        hbox = QHBoxLayout()
        hbox.addWidget(self.fee_slider)
        hbox.addWidget(self.fee_custom_lbl)
        hbox.addWidget(self.fee_e)
        hbox.addStretch(1)
        grid.addLayout(hbox, 9, 1)

        self.preview_button = EnterButton(_("&Preview"), self.do_preview)
        self.preview_button.setToolTip(_('Display the details of your transactions before signing it.'))
        self.send_button = EnterButton(_("&Send"), self.do_send)
        self.cointext_button = EnterButton(_("Coin&Text"), self.do_cointext)
        self.cointext_button.setToolTip(_('Process CoinText, transforming it into a BIP70 payment request.'))
        self.clear_button = EnterButton(_("&Clear"), self.do_clear)
        buttons = QHBoxLayout()
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.cointext_button)
        buttons.addStretch(1)
        grid.addLayout(buttons, 11, 1, 1, 3)

        self.payto_e.textChanged.connect(self.update_buttons_on_seed)  # hide/unhide cointext button, etc

        self.amount_e.shortcut.connect(self.spend_max)
        self.payto_e.textChanged.connect(self.update_fee)
        self.amount_e.textEdited.connect(self.update_fee)
        self.message_opreturn_e.textEdited.connect(self.update_fee)
        self.message_opreturn_e.textChanged.connect(self.update_fee)
        self.message_opreturn_e.editingFinished.connect(self.update_fee)
        self.opreturn_rawhex_cb.stateChanged.connect(self.update_fee)

        def reset_max(text):
            self.max_button.setChecked(False)
            if not self.slp_token_id:
                enabled = not bool(text) and not self.amount_e.isReadOnly()
                self.max_button.setEnabled(enabled)
        self.amount_e.textEdited.connect(reset_max)
        self.fiat_send_e.textEdited.connect(reset_max)

        def entry_changed():
            if self.is_slp_wallet:
                hasError = entry_changed_slp()
                if hasError == False:
                    entry_changed_bch()
            else:
                entry_changed_bch()

        def entry_changed_bch():
            text = ""
            if self.not_enough_funds:
                amt_color, fee_color = ColorScheme.RED, ColorScheme.RED
                text = _( "Not enough BCH" )
                c, u, x = self.wallet.get_frozen_balance()
                if c+u+x:
                    text += ' (' + self.format_amount(c+u+x).strip() + ' ' + self.base_unit() + ' ' +_("are frozen") + ')'
                slp = self.wallet.get_slp_locked_balance()
                if slp > 0:
                    text += " (" + self.format_amount(slp).strip() + " BCH held in tokens)"
                extra = run_hook("not_enough_funds_extra", self)
                if isinstance(extra, str) and extra:
                    text += " ({})".format(extra)

            elif self.fee_e.isModified():
                amt_color, fee_color = ColorScheme.DEFAULT, ColorScheme.DEFAULT
            elif self.amount_e.isModified():
                amt_color, fee_color = ColorScheme.DEFAULT, ColorScheme.BLUE
            else:
                amt_color, fee_color = ColorScheme.BLUE, ColorScheme.BLUE
            opret_color = ColorScheme.DEFAULT
            if self.op_return_toolong:
                opret_color = ColorScheme.RED
                text = _("OP_RETURN message too large, needs to be no longer than 220 bytes") + (", " if text else "") + text

            self.statusBar().showMessage(text)
            self.amount_e.setStyleSheet(amt_color.as_stylesheet())
            self.fee_e.setStyleSheet(fee_color.as_stylesheet())
            self.message_opreturn_e.setStyleSheet(opret_color.as_stylesheet())

        self.amount_e.textChanged.connect(entry_changed)
        self.fee_e.textChanged.connect(entry_changed)
        self.message_opreturn_e.textChanged.connect(entry_changed)
        self.message_opreturn_e.textEdited.connect(entry_changed)
        self.message_opreturn_e.editingFinished.connect(entry_changed)
        self.opreturn_rawhex_cb.stateChanged.connect(entry_changed)
        if self.is_slp_wallet:
            self.slp_amount_e.textChanged.connect(entry_changed)
            self.slp_amount_e.editingFinished.connect(entry_changed)

        def entry_changed_slp():
            if self.token_type_combo.currentData():
                text = ""
                name = self.wallet.token_types.get(self.slp_token_id)['name']
                decimals = self.wallet.token_types.get(self.slp_token_id)['decimals']
                if self.not_enough_funds_slp or self.not_enough_unfrozen_funds_slp:
                    bal_avail, _, _, _, frozen_amt = self.wallet.get_slp_token_balance(self.slp_token_id, { 'user_config': { 'confirmed_only': False }})
                    if self.not_enough_funds_slp:
                        amt_color = ColorScheme.RED
                        text = "Not enough " + \
                                name + " tokens (" + \
                                format_satoshis_plain_nofloat(bal_avail, decimals) + " valid"
                        if self.config.get('confirmed_only', False):
                            conf_bal_avail = self.wallet.get_slp_token_balance(self.slp_token_id, self.config)[0]
                            unconf_bal = bal_avail - conf_bal_avail
                            if unconf_bal > 0:
                                text += ", " + format_satoshis_plain_nofloat(unconf_bal, decimals) + " unconfirmed)"
                            else:
                                text += ")"
                        else:
                            text += ")"
                    elif self.not_enough_unfrozen_funds_slp:
                        amt_color = ColorScheme.RED
                        text = "Not enough unfrozen " + name + " tokens (" + \
                                format_satoshis_plain_nofloat(bal_avail, decimals) + " valid, " + \
                                format_satoshis_plain_nofloat(frozen_amt, decimals) + " frozen)"
                elif self.slp_amount_e.isModified():
                    amt_color = ColorScheme.DEFAULT
                else:
                    amt_color = ColorScheme.BLUE

                try:
                    if self.slp_amount_e.get_amount() > (2 ** 64) - 1:
                        amt_color = ColorScheme.RED
                        maxqty = format_satoshis_plain_nofloat((2 ** 64) - 1, self.wallet.token_types.get(self.slp_token_id)['decimals'])
                        text = _("Token output quantity is too large. Maximum %s.")%(maxqty,)
                except TypeError:
                    pass

                self.statusBar().showMessage(text)
                self.slp_amount_e.setStyleSheet(amt_color.as_stylesheet())
                if text != "":
                    return True
            return False

        self.invoices_label = QLabel(_('Invoices'))
        from .invoice_list import InvoiceList
        self.invoice_list = InvoiceList(self)
        self.invoice_list.chkVisible()

        vbox0 = QVBoxLayout()
        vbox0.addLayout(grid)
        hbox = QHBoxLayout()
        hbox.addLayout(vbox0)

        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        vbox.addWidget(self.invoices_label)
        vbox.addWidget(self.invoice_list)
        vbox.setStretchFactor(self.invoice_list, 1000)
        w.searchable_list = self.invoice_list
        run_hook('create_send_tab', grid)
        return w

    def spend_max(self):
        self.max_button.setChecked(True)
        self.do_update_fee()

    def slp_spend_max(self):
        self.slp_amount_e.setAmount(self.wallet.get_slp_token_balance(self.slp_token_id, self.config)[3])
        self.do_update_fee()

    def update_fee(self):
        self.require_fee_update = True

    def get_payto_or_dummy(self):
        r = self.payto_e.get_recipient()
        if r:
            return r
        return (TYPE_ADDRESS, self.wallet.dummy_address())

    def get_custom_fee_text(self, fee_rate = None):
        if not self.config.has_custom_fee_rate():
            return ""
        else:
            if fee_rate is None: fee_rate = self.config.custom_fee_rate() / 1000.0
            return str(round(fee_rate*100)/100) + " sats/B"

    @staticmethod
    def output_for_opreturn_stringdata(op_return):
        if not isinstance(op_return, str):
            raise OPReturnError('OP_RETURN parameter needs to be of type str!')
        pushes = op_return.split('<push>')
        script = "OP_RETURN"
        for data in pushes:
            if data.startswith("<hex>"):
                data = data.replace("<hex>", "")
            elif data.startswith("<empty>"):
                pass
            else:
                data = data.encode('utf-8').hex()
            script = script + " " + data
        scriptBuffer = ScriptOutput.from_string(script)
        if len(scriptBuffer.script) > 223:
            raise OPReturnTooLarge(_("OP_RETURN message too large, needs to be no longer than 220 bytes"))
        amount = 0
        return (TYPE_SCRIPT, scriptBuffer, amount)

    @staticmethod
    def output_for_opreturn_rawhex(op_return):
        if not isinstance(op_return, str):
            raise OPReturnError('OP_RETURN parameter needs to be of type str!')
        if op_return == 'empty':
            op_return = ''
        try:
            op_return_script = b'\x6a' + bytes.fromhex(op_return.strip())
        except ValueError:
            raise OPReturnError(_('OP_RETURN script expected to be hexadecimal bytes'))
        if len(op_return_script) > 223:
            raise OPReturnTooLarge(_("OP_RETURN script too large, needs to be no longer than 223 bytes"))
        amount = 0
        return (TYPE_SCRIPT, ScriptOutput(op_return_script), amount)

    def do_update_fee(self):
        '''Recalculate the fee.  If the fee was manually input, retain it, but
        still build the TX to see if there are enough funds.
        '''
        bch_outputs = []
        token_output_amts = []
        self.not_enough_funds = False
        self.not_enough_funds_slp = False
        self.not_enough_unfrozen_funds_slp = False
        freeze_fee = (self.fee_e.isModified()
                      and (self.fee_e.text() or self.fee_e.hasFocus()))
        amount = '!' if self.max_button.isChecked() else self.amount_e.get_amount()
        fee_rate = None
        if self.is_slp_wallet:
            slp_amount = self.slp_amount_e.get_amount()
            if amount is None and slp_amount is None:
                if not freeze_fee:
                    self.fee_e.setAmount(None)
                self.statusBar().showMessage('')
                return
        else:
            if amount is None:
                if not freeze_fee:
                    self.fee_e.setAmount(None)
                self.statusBar().showMessage('')
                return

        try:
            selected_slp_coins = []
            if self.slp_token_id:
                amt = slp_amount or 0
                selected_slp_coins, slp_op_return_msg = SlpCoinChooser.select_coins(self.wallet, self.slp_token_id, amt, self.config)
                if slp_op_return_msg:
                    bch_outputs = [ slp_op_return_msg ]
                    token_output_amts = slp.SlpMessage.parseSlpOutputScript(bch_outputs[0][1]).op_return_fields['token_output']
                    for amt in token_output_amts:
                        # just grab a dummy address for this fee calculation - safe for imported_privkey wallets
                        bch_outputs.append((TYPE_ADDRESS, self.wallet.get_addresses()[0], 546))

            bch_payto_outputs = self.payto_e.get_outputs(self.max_button.isChecked())
            if bch_payto_outputs and bch_payto_outputs[0][2]:
                bch_outputs.extend(bch_payto_outputs)
            elif self.slp_token_id and amount and not bch_payto_outputs:
                _type, addr = self.get_payto_or_dummy()
                bch_outputs.append((_type, addr, amount))
            if not bch_outputs:
                _type, addr = self.get_payto_or_dummy()
                bch_outputs.append((_type, addr, amount))

            if not self.slp_token_id:
                opreturn_message = self.message_opreturn_e.text() if self.config.get('enable_opreturn') else None
                if (opreturn_message != '' and opreturn_message is not None):
                    if self.opreturn_rawhex_cb.isChecked():
                        bch_outputs.insert(0, self.output_for_opreturn_rawhex(opreturn_message))
                    else:
                        bch_outputs.insert(0, self.output_for_opreturn_stringdata(opreturn_message))

            fee = self.fee_e.get_amount() if freeze_fee else None
            tx = self.wallet.make_unsigned_transaction(self.get_coins(isInvoice = False), bch_outputs, self.config, fee, mandatory_coins=selected_slp_coins)
            if self.slp_token_id:
                self.wallet.check_sufficient_slp_balance(slp.SlpMessage.parseSlpOutputScript(slp_op_return_msg[1]), self.config)
            self.not_enough_funds = False
            self.op_return_toolong = False
        except NotEnoughFunds:
            self.not_enough_funds = True
            if not freeze_fee:
                self.fee_e.setAmount(None)
            return
        except NotEnoughFundsSlp:
            self.not_enough_funds_slp = True
            if not freeze_fee:
                self.fee_e.setAmount(None)
            return
        except NotEnoughUnfrozenFundsSlp:
            self.not_enough_unfrozen_funds_slp = True
            if not freeze_fee:
                self.fee_e.setAmount(None)
            return
        except OPReturnTooLarge:
            self.op_return_toolong = True
            return
        except OPReturnError as e:
            self.statusBar().showMessage(str(e))
            return
        except BaseException:
            return

        if not freeze_fee:
            fee = None if self.not_enough_funds else tx.get_fee()
            if not self.slp_token_id or len(token_output_amts) > 0:
                self.fee_e.setAmount(fee)

        if self.max_button.isChecked():
            amount = tx.output_value()
            if self.is_slp_wallet:
                amount = tx.output_value() - len(token_output_amts) * 546
            self.amount_e.setAmount(amount)
        if fee is not None:
            fee_rate = fee / tx.estimated_size()
        self.fee_slider_mogrifier(self.get_custom_fee_text(fee_rate))

    def fee_slider_mogrifier(self, text = None):
        fee_slider_hidden = self.config.has_custom_fee_rate()
        self.fee_slider.setHidden(fee_slider_hidden)
        self.fee_custom_lbl.setHidden(not fee_slider_hidden)
        if text is not None: self.fee_custom_lbl.setText(text)

    def from_list_delete(self, item):
        i = self.from_list.indexOfTopLevelItem(item)
        self.pay_from.pop(i)
        self.redraw_from_list()
        self.update_fee()

    def from_list_menu(self, position):
        item = self.from_list.itemAt(position)
        if not item:
            return
        menu = QMenu()
        menu.addAction(_("Remove"), lambda: self.from_list_delete(item))
        menu.exec_(self.from_list.viewport().mapToGlobal(position))

    def set_pay_from(self, coins):
        self.pay_from = list(coins)
        self.redraw_from_list()

    def redraw_from_list(self):
        self.from_list.clear()
        self.from_label.setHidden(len(self.pay_from) == 0)
        self.from_list.setHidden(len(self.pay_from) == 0)

        def format(x):
            h = x['prevout_hash']
            return '{}...{}:{:d}\t{}'.format(h[0:10], h[-10:],
                                             x['prevout_n'], x['address'])

        for item in self.pay_from:
            self.from_list.addTopLevelItem(QTreeWidgetItem( [format(item), self.format_amount(item['value']) ]))

    def get_contact_payto(self, key):
        _type, label = self.contacts.get(key)
        return label + '  <' + key + '>' if _type == 'address' else key

    def update_completions(self):
        l = [self.get_contact_payto(key) for key in self.contacts.keys()]
        self.completions.setStringList(l)

    def protected(func):
        '''Password request wrapper.  The password is passed to the function
        as the 'password' named argument.  "None" indicates either an
        unencrypted wallet, or the user cancelled the password request.
        An empty input is passed as the empty string.'''
        def request_password(self, *args, **kwargs):
            parent = self.top_level_window()
            password = None
            on_pw_cancel = kwargs.pop('on_pw_cancel', None)
            while self.wallet.has_password():
                password = self.password_dialog(parent=parent)
                if password is None:
                    # User cancelled password input
                    if callable(on_pw_cancel):
                        on_pw_cancel()
                    return
                try:
                    self.wallet.check_password(password)
                    break
                except Exception as e:
                    self.show_error(str(e), parent=parent)
                    continue

            kwargs['password'] = password
            return func(self, *args, **kwargs)
        return request_password

    def read_send_tab(self, preview=False):
        bch_outputs = []
        selected_slp_coins = []
        opreturn_message = self.message_opreturn_e.text() if self.config.get('enable_opreturn') else None
        if self.slp_token_id:
            if self.slp_amount_e.get_amount() == 0 or self.slp_amount_e.get_amount() is None:
                self.show_message(_("No SLP token amount provided."))
                return
            try:
                """ Guard against multiline 'Pay To' field """
                if self.payto_e.is_multiline():
                    self.show_error(_("Too many receivers listed.\n\nCurrently this wallet only supports a single SLP token receiver."))
                    return
                """ Guard against bad address encoding """
                if not self.payto_e.payto_address:
                    self.show_error(_("Enter SLP address."))
                    return
                """ Require SLPADDR prefix in 'Pay To' field. """
                if networks.net.SLPADDR_PREFIX not in self.payto_e.address_string_for_slp_check:
                    self.show_error(_("Address provided is not in SLP Address format.\n\nThe address should be encoded using 'simpleledger:' or 'slptest:' URI prefix."))
                    return
                amt = self.slp_amount_e.get_amount()
                selected_slp_coins, slp_op_return_msg = SlpCoinChooser.select_coins(self.wallet, self.slp_token_id, amt, self.config)
                if slp_op_return_msg:
                    bch_outputs = [ slp_op_return_msg ]
            except OPReturnTooLarge as e:
                self.show_error(str(e))
                return
            except OPReturnError as e:
                self.show_error(str(e))
                return

        isInvoice= False

        if self.payment_request and self.payment_request.has_expired():
            self.show_error(_('Payment request has expired'))
            return
        label = self.message_e.text()

        if self.payment_request:
            if self.slp_token_id:
                self.show_error('BIP-70 Payment requests are not yet working for SLP tokens.')
                return
            isInvoice = True
            bch_outputs.extend(self.payment_request.get_outputs())
        else:
            errors = self.payto_e.get_errors()
            if errors:
                self.show_warning(_("Invalid lines found:") + "\n\n" + '\n'.join([ _("Line #") + str(x[0]+1) + ": " + x[1] for x in errors]))
                return
            if self.slp_token_id:
                _type, _addr = self.payto_e.payto_address
                bch_outputs.append((_type, _addr, 546))

            if self.payto_e.is_alias and self.payto_e.validated is False:
                alias = self.payto_e.toPlainText()
                msg = _('WARNING: the alias "{}" could not be validated via an additional '
                        'security check, DNSSEC, and thus may not be correct.').format(alias) + '\n'
                msg += _('Do you wish to continue?')
                if not self.question(msg):
                    return

        coins = self.get_coins(isInvoice=isInvoice)

        """ SLP: Add an additional token change output """
        if self.slp_token_id:
            change_addr = None
            token_outputs = slp.SlpMessage.parseSlpOutputScript(bch_outputs[0][1]).op_return_fields['token_output']
            if len(token_outputs) > 1 and len(bch_outputs) - 1 < len(token_outputs):
                """ start of logic copied from wallet.py """
                addrs = self.wallet.get_change_addresses()[-self.wallet.gap_limit_for_change:]
                if self.wallet.use_change and addrs:
                    # New change addresses are created only after a few
                    # confirmations.  Select the unused addresses within the
                    # gap limit; if none take one at random
                    change_addrs = [addr for addr in addrs if
                                    self.wallet.get_num_tx(addr) == 0]
                    if not change_addrs:
                        import random
                        change_addrs = [random.choice(addrs)]
                        change_addr = change_addrs[0]
                    elif len(change_addrs) > 1:
                        change_addr = change_addrs[1]
                    else:
                        change_addr = change_addrs[0]
                else:
                    change_addr = coins[0]['address']
                bch_outputs.append((TYPE_ADDRESS, change_addr, 546))

        # add normal BCH amounts
        if not self.payment_request and self.amount_e.get_amount():
            bch_outputs.extend(self.payto_e.get_outputs(self.max_button.isChecked()))

        """ Only Allow OP_RETURN if SLP is disabled. """
        if not self.slp_token_id:
            try:
                # handle op_return if specified and enabled
                opreturn_message = self.message_opreturn_e.text()
                if opreturn_message:
                    if self.opreturn_rawhex_cb.isChecked():
                        bch_outputs.append(self.output_for_opreturn_rawhex(opreturn_message))
                    else:
                        bch_outputs.append(self.output_for_opreturn_stringdata(opreturn_message))
            except OPReturnTooLarge as e:
                self.show_error(str(e))
                return
            except OPReturnError as e:
                self.show_error(str(e))
                return


        if not bch_outputs:
            self.show_error(_('Enter receiver address (No BCH outputs).'))
            return

        for _type, addr, amount in bch_outputs:
            if amount is None:
                self.show_error(_('Invalid Amount'))
                return

        freeze_fee = self.fee_e.isVisible() and self.fee_e.isModified() and (self.fee_e.text() or self.fee_e.hasFocus())
        fee = self.fee_e.get_amount() if freeze_fee else None
        return bch_outputs, fee, label, coins, selected_slp_coins

    _cointext_popup_kill_tab_changed_connection = None
    def do_cointext(self):
        ''' This is called by the cointext button 'clicked' signal and it
        initiates the processing of the cointext URL.  This should only be
        called if self.payto_e.cointext is not None, otherwise it will do
        nothing. '''
        if self.payto_e.cointext and not self.payment_request:
            if self.gui_object.warn_if_no_network(self):
                return
            phone = self.payto_e.cointext
            sats = self.amount_e.get_amount()
            if sats:
                url = "https://pay.cointext.io/p/{}/{}".format(phone, sats)
                def get_cointext_pr():
                    # Runs in thread
                    self.print_error("CoinText URL", url)
                    pr = paymentrequest.get_payment_request(url)  # raises on error
                    return pr
                def on_success(pr):
                    # Runs in main thread
                    if pr:
                        if pr.error:
                            self.print_error("CoinText ERROR", pr.error)
                            self.show_error(_("There was an error processing the CoinText. Please check the phone number and try again."))
                            return
                        self.print_error("CoinText RESULT", repr(pr))
                        self.prepare_for_payment_request()
                        def show_popup():
                            if not self.send_button.isVisible():
                                # likely a watching-only wallet, in which case
                                # showing the popup label for the send button
                                # leads to unspecified position for the button
                                return
                            show_it = partial(
                                        ShowPopupLabel,
                                        text=_("Please review payment before sending CoinText"),
                                        target=self.send_button, timeout=15000.0,
                                        name="CoinTextPopup",
                                        pointer_position=PopupWidget.LeftSide,
                                        activation_hides=True, track_target=True,
                                        dark_mode = ColorScheme.dark_scheme
                            )
                            if not self._cointext_popup_kill_tab_changed_connection:
                                # this ensures that if user changes tabs, the popup dies
                                # ... it is only connected once per instance lifetime
                                self._cointext_popup_kill_tab_changed_connection = self.tabs.currentChanged.connect(lambda: KillPopupLabel("CoinTextPopup"))
                            QTimer.singleShot(0, show_it)
                        pr.request_ok_callback = show_popup
                        self.on_pr(pr)
                def on_error(exc):
                    self.print_error("CoinText EXCEPTION", repr(exc))
                    self.on_error(exc)
                WaitingDialog(self.top_level_window(),
                              _("Retrieving CoinText info, please wait ..."),
                              get_cointext_pr, on_success, on_error)
            else:
                self.show_error(_('CoinText: Please specify an amount'))

    def do_preview(self):
        self.do_send(preview = True)

    def do_send(self, preview = False):
        if run_hook('abort_send', self):
            return

        r = self.read_send_tab(preview=preview)

        if not r:
            return
        outputs, fee, tx_desc, coins, slp_coins = r

        if self.slp_token_id:
            try:
                self.wallet.check_sufficient_slp_balance(slp.SlpMessage.parseSlpOutputScript(outputs[0][1]), self.config)
            except slp.SlpInvalidOutputMessage:
                self.show_message(_("No token outputs available.\n\nIf you have unconfirmed tokens wait 1 confirmation or turn off 'Spend only confirmed coins' in preferences, and try again."))
                return
            except NotEnoughFundsSlp:
                self.show_message(_("Token balance too low."))
                return
            except NotEnoughUnfrozenFundsSlp:
                self.show_message(_("Unfrozen SLP token balance is too low.  Unfreeze some of the token coins associated with with this token."))
                return

        try:
            tx = self.wallet.make_unsigned_transaction(coins, outputs, self.config, fee, mandatory_coins=slp_coins)
        except NotEnoughFunds:
            self.show_message(_("Insufficient BCH balance"))
            return
        except ExcessiveFee:
            self.show_message(_("Your fee is too high.  Max is 50 sat/byte."))
            return
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        amount = tx.output_value() if self.max_button.isChecked() else sum(map(lambda x:x[2], outputs))
        fee = tx.get_fee()

        #if fee < self.wallet.relayfee() * tx.estimated_size() / 1000 and tx.requires_fee(self.wallet):
            #self.show_error(_("This transaction requires a higher fee, or it will not be propagated by the network"))
            #return

        if preview:
            self.show_transaction(tx, tx_desc)
            return

        # confirmation dialog
        if self.slp_token_id:
            slp_amt_str = format_satoshis_plain_nofloat(self.slp_amount_e.get_amount(), self.wallet.token_types.get(self.slp_token_id)['decimals'])
            slp_name = self.wallet.token_types[self.slp_token_id]['name']
            msg = [
                _("BCH amount to be sent") + ": " + self.format_amount_and_units(amount),
                "\nToken amount to be sent" + ": " + slp_amt_str + " " + slp_name,
                _("\nMining fee") + ": " + self.format_amount_and_units(fee),
            ]
        else:
            msg = [
                _("\nAmount to be sent") + ": " + self.format_amount_and_units(amount),
                _("\nMining fee") + ": " + self.format_amount_and_units(fee),
            ]

        x_fee = run_hook('get_tx_extra_fee', self.wallet, tx)
        if x_fee:
            x_fee_address, x_fee_amount = x_fee
            msg.append( _("\nAdditional fees") + ": " + self.format_amount_and_units(x_fee_amount) )

        confirm_rate = 2 * self.config.max_fee_rate()

        # IN THE FUTURE IF WE WANT TO APPEND SOMETHING IN THE MSG ABOUT THE FEE, CODE IS COMMENTED OUT:
        #if fee > confirm_rate * tx.estimated_size() / 1000:
        #    msg.append(_('Warning') + ': ' + _("The fee for this transaction seems unusually high."))

        if (fee < (tx.estimated_size())):
            msg.append(_('\nWarning') + ': ' + _("You're using a fee of less than 1.0 sats/B. It may take a very long time to confirm."))
            tx.ephemeral['warned_low_fee_already'] = True

        if self.config.get('enable_opreturn') and self.message_opreturn_e.text():
            msg.append(_("\nYou are using an OP_RETURN message. This gets permanently written to the blockchain."))

        if self.wallet.has_password():
            msg.append("")
            msg.append(_("\nEnter your password to proceed"))
            password = self.password_dialog('\n'.join(msg))
            if not password:
                return
        else:
            msg.append(_('\nProceed?'))
            password = None
            if not self.question('\n'.join(msg)):
                return

        def sign_done(success):
            if success:
                if not tx.is_complete():
                    self.show_transaction(tx, tx_desc)
                    self.do_clear()
                else:
                    self.broadcast_transaction(tx, tx_desc)
        self.sign_tx_with_password(tx, sign_done, password)

    @protected
    def sign_tx(self, tx, callback, password, *, slp_coins_to_burn=None):
        self.sign_tx_with_password(tx, callback, password, slp_coins_to_burn=slp_coins_to_burn)

    def sign_tx_with_password(self, tx, callback, password, *, slp_coins_to_burn=None):
        '''Sign the transaction in a separate thread.  When done, calls
        the callback with a success code of True or False.
        '''
        
        # check transaction SLP validity before signing
        try:
            assert SlpTransactionChecker.check_tx_slp(self.wallet, tx, coins_to_burn=slp_coins_to_burn)
        except (Exception, AssertionError) as e:
            self.show_warning(str(e))
            return   
                 
        # call hook to see if plugin needs gui interaction
        run_hook('sign_tx', self, tx)

        def on_signed(result):
            callback(True)
        def on_failed(exc_info):
            self.on_error(exc_info)
            callback(False)

        if self.tx_external_keypairs:
            task = partial(Transaction.sign, tx, self.tx_external_keypairs)
        else:
            task = partial(self.wallet.sign_transaction, tx, password)
        WaitingDialog(self, _('Signing transaction...'), task,
                      on_signed, on_failed)

    def broadcast_transaction(self, tx, tx_desc):

        def broadcast_thread():
            # non-GUI thread
            status = False
            msg = "Failed"
            pr = self.payment_request
            if pr and pr.has_expired():
                self.payment_request = None
                return False, _("Payment request has expired")
            if pr:
                refund_address = self.wallet.get_receiving_addresses()[0]
                ack_status, ack_msg = pr.send_payment(str(tx), refund_address)
                msg = ack_msg
                if ack_status:
                    self.invoices.set_paid(pr, tx.txid())
                    self.invoices.save()
                    self.payment_request = None
                    status = True
            else:
                status, msg =  self.network.broadcast_transaction(tx)
            return status, msg

        # Check fee and warn if it's below 1.0 sats/B (and not warned already)
        fee = None
        try: fee = tx.get_fee()
        except: pass # no fee info available for tx
        # Check fee >= size otherwise warn. FIXME: If someday network relay
        # rules change to be other than 1.0 sats/B minimum, this code needs
        # to be changed.
        if (isinstance(fee, int) and tx.is_complete() and fee < len(str(tx))//2
                and not tx.ephemeral.get('warned_low_fee_already')):
            msg = _('Warning') + ': ' + _("You're using a fee of less than 1.0 sats/B. It may take a very long time to confirm.") + "\n\n" + _("Proceed?")
            if not self.question(msg, title = _("Low Fee")):
                return
        # /end fee check

        # Capture current TL window; override might be removed on return
        parent = self.top_level_window()

        if self.gui_object.warn_if_no_network(self):
            # Don't allow a useless broadcast when in offline mode. Previous to this we were getting an exception on broadcast.
            return
        elif not self.network.is_connected():
            # Don't allow a potentially very slow broadcast when obviously not connected.
            parent.show_error(_("Not connected"))
            return

        def broadcast_done(result):
            # GUI thread
            if result:
                status, msg = result
                if status:
                    buttons, copy_index, copy_link = [ _('Ok') ], None, ''
                    try: txid = tx.txid()  # returns None if not is_complete, but may raise potentially as well
                    except: txid = None
                    if txid is not None:
                        if tx_desc is not None:
                            self.wallet.set_label(txid, tx_desc)
                        copy_link = web.BE_URL(self.config, 'tx', txid)
                        if copy_link:
                            # tx is complete and there is a copy_link
                            buttons.insert(0, _("Copy link"))
                            copy_index = 0
                    if parent.show_message(_('Payment sent.') + '\n' + msg,
                                           buttons = buttons,
                                           defaultButton = buttons[-1],
                                           escapeButton = buttons[-1]) == copy_index:
                        # There WAS a 'Copy link' and they clicked it
                        self.copy_to_clipboard(copy_link, _("Block explorer link copied to clipboard"), self.top_level_window())
                    self.invoice_list.update()
                    self.do_clear()
                else:
                    if msg.startswith("error: "):
                        msg = msg.split(" ", 1)[-1] # take the last part, sans the "error: " prefix
                    parent.show_error(msg)

        WaitingDialog(self, _('Broadcasting transaction...'),
                      broadcast_thread, broadcast_done, self.on_error)

    def query_choice(self, msg, choices):
        # Needed by QtHandler for hardware wallets
        dialog = WindowModalDialog(self.top_level_window())
        clayout = ChoicesLayout(msg, choices)
        vbox = QVBoxLayout(dialog)
        vbox.addLayout(clayout.layout())
        vbox.addLayout(Buttons(OkButton(dialog)))
        result = dialog.exec_()
        dialog.setParent(None)
        if not result:
            return None
        return clayout.selected_index()

    def lock_amount(self, b):
        pass
        # the following is now not needed since slp dust amounts are now hard coded
        '''
        This if-statement was added for SLP around the following two lines
        in order to keep the amount field locked and Max button disabled
        when the payto field is edited when a token is selected.
        '''
        # if self.is_slp_wallet and self.token_type_combo.currentData():
        #     self.amount_e.setFrozen(True)
        #     self.max_button.setEnabled(False)

    def prepare_for_payment_request(self):
        self.show_send_tab()
        self.payto_e.cointext = None
        self.payto_e.is_pr = True
        for e in [self.payto_e, self.amount_e, self.message_e]:
            e.setFrozen(True)

        # Note: the below loop freezes all SLP widgets if present in the send
        # tab; redo this when BIP70 supports SLP token sends. -Calin
        for e in self.slp_send_tab_widgets:
            e.setDisabled(True)

        if self.is_slp_wallet:
            # force SLP token type to 0 for payment requests
            self.token_type_combo.setCurrentIndex(0)

        self.max_button.setDisabled(True)
        self.payto_e.setText(_("please wait..."))
        return True

    def delete_invoice(self, key):
        self.invoices.remove(key)
        self.invoice_list.update()

    def payment_request_ok(self):
        pr = self.payment_request
        key = self.invoices.add(pr)
        status = self.invoices.get_status(key)
        self.invoice_list.update()
        if status == PR_PAID:
            self.show_message("invoice already paid")
            self.do_clear()
            self.payment_request = None
            return
        self.payto_e.is_pr = True
        if not pr.has_expired():
            self.payto_e.setGreen()
        else:
            self.payto_e.setExpired()
        self.payto_e.setText(pr.get_requestor())
        self.amount_e.setText(format_satoshis_plain(pr.get_amount(), self.decimal_point))
        self.message_e.setText(pr.get_memo())
        # signal to set fee
        self.amount_e.textEdited.emit("")
        # New! Payment requests have an optional (may not be there!) attribute
        # 'request_ok_callback' which takes 0 args and is called on request ok
        # This facility was needed to do the CoinTextPopup label properly.
        cb = getattr(self.payment_request, 'request_ok_callback', None)
        if callable(cb):
            cb()

    def payment_request_error(self):
        request_error = self.payment_request and self.payment_request.error
        self.payment_request = None
        self.print_error("PaymentRequest error:", request_error)
        self.show_error(_("There was an error processing the payment request"), rich_text=False)
        self.do_clear()

    def on_pr(self, request):
        self.payment_request = request
        if self.payment_request.verify(self.contacts):
            self.payment_request_ok_signal.emit()
        else:
            self.payment_request_error_signal.emit()

    def pay_to_URI(self, URI):
        self.do_clear()
        if not URI:
            return
        try:
            out = web.parse_URI(URI, self.on_pr)
        except Exception as e:
            if 'ms-python' in URI:  # this is needed for visual studio code debugger
                return
            self.show_error(_('Invalid Address URI:') + '\n' + str(e))
            return
        self.show_send_tab()
        r = out.get('r')
        sig = out.get('sig')
        name = out.get('name')
        if r or (name and sig):
            self.prepare_for_payment_request()
            return
        scheme = out.get('scheme')
        address = out.get('address')
        amounts = out.get('amounts')
        label = out.get('label')
        message = out.get('message')
        op_return = out.get('op_return')
        op_return_raw = out.get('op_return_raw')

        # use label as description (not BIP21 compliant)
        if label and not message:
            message = label
        if address:
            self.payto_e.setText(URI.split('?')[0])
        if message:
            self.message_e.setText(message)
        if amounts:
            if scheme == networks.net.CASHADDR_PREFIX and 'bch' in amounts:
                self.amount_e.setAmount(amounts['bch']['amount'])
                self.amount_e.textEdited.emit("")
            elif self.is_slp_wallet and scheme == networks.net.SLPADDR_PREFIX:
                # pick first token in amounts
                tokenid = None
                for key in amounts:
                    if key != 'bch':
                        tokenid = key
                        index = 1
                        while index < self.token_type_combo.count():
                            self.token_type_combo.setCurrentIndex(index)
                            if self.token_type_combo.currentData() == tokenid:
                                break
                            index+=1
                        if index == self.token_type_combo.count():
                            self.token_type_combo.setCurrentIndex(0)
                            self.show_error(_("Token is not in your wallet, check that you have added this token in the 'Tokens' tab"))
                            return
                        self.slp_amount_e.setAmount(amounts[tokenid]['amount'] * pow(10, self.slp_amount_e.token_decimals))
                        self.slp_amount_e.textEdited.emit("")
                        break
                if tokenid == None and 'bch' in amounts:
                    self.amount_e.setAmount(amounts['bch']['amount'])
                    self.amount_e.textEdited.emit("")
                elif 'bch' in amounts:
                    self.amount_e.setAmount(amounts['bch']['amount'])
                    self.amount_e.textEdited.emit("")
                    self.slp_extra_bch_cb.setChecked(True)
                    self.slp_extra_bch_cb.clicked.emit()
            else:
                self.show_error("Unsupported URI prefix: " + scheme)

        if op_return:
            self.message_opreturn_e.setText(op_return)
            self.message_opreturn_e.setHidden(False)
            self.opreturn_rawhex_cb.setHidden(False)
            self.opreturn_rawhex_cb.setChecked(False)
            self.opreturn_label.setHidden(False)
        elif op_return_raw is not None:
            # 'is not None' allows blank value.
            # op_return_raw is secondary precedence to op_return
            if not op_return_raw:
                op_return_raw='empty'
            self.message_opreturn_e.setText(op_return_raw)
            self.message_opreturn_e.setHidden(False)
            self.opreturn_rawhex_cb.setHidden(False)
            self.opreturn_rawhex_cb.setChecked(True)
            self.opreturn_label.setHidden(False)
        elif not self.config.get('enable_opreturn'):
            self.message_opreturn_e.setText('')
            self.message_opreturn_e.setHidden(True)
            self.opreturn_rawhex_cb.setHidden(True)
            self.opreturn_label.setHidden(True)

    def do_clear(self):
        ''' Clears the send tab, reseting its UI state to its initiatial state.'''
        for e in [self.payto_e, self.message_e, self.amount_e, self.fiat_send_e, self.fee_e, self.message_opreturn_e]:
            e.setText('')
            e.setFrozen(False)
        self.max_button.setDisabled(False)
        KillPopupLabel("CoinTextPopup")  # just in case it was alive
        self.max_button.setChecked(False)
        self.not_enough_funds = False
        self.op_return_toolong = False
        self.payment_request = None
        self.payto_e.cointext = None
        self.payto_e.is_pr = False
        self.opreturn_rawhex_cb.setChecked(False)
        self.set_pay_from([])
        self.tx_external_keypairs = {}
        self.message_opreturn_e.setVisible(self.config.get('enable_opreturn', False))
        self.opreturn_rawhex_cb.setVisible(self.config.get('enable_opreturn', False))
        self.opreturn_label.setVisible(self.config.get('enable_opreturn', False))
        self.update_status()
        self.amount_e.setHidden(False)
        self.amount_label.setHidden(False)
        if self.is_slp_wallet:
            self.not_enough_funds_slp = False
            self.not_enough_unfrozen_funds_slp = False
            for e in self.slp_send_tab_widgets:
                e.setDisabled(False)
            self.slp_amount_e.setText('')
            self.token_type_combo.setCurrentIndex(0)
            self.on_slptok() # resets parts of the send tab to initial state
        run_hook('do_clear', self)

    def set_frozen_state(self, addrs, freeze):
        self.wallet.set_frozen_state(addrs, freeze)
        self.address_list.update()
        self.utxo_list.update()
        self.update_fee()

    def set_frozen_coin_state(self, utxos, freeze):
        self.wallet.set_frozen_coin_state(utxos, freeze)
        self.utxo_list.update()
        self.update_fee()

    def create_converter_tab(self):

        source_address = QLineEdit()
        cash_address = ButtonsLineEdit()
        cash_address.addCopyButton()
        cash_address.setReadOnly(True)
        legacy_address = ButtonsLineEdit()
        legacy_address.addCopyButton()
        legacy_address.setReadOnly(True)
        slp_address = ButtonsLineEdit()
        slp_address.setReadOnly(True)
        slp_address.addCopyButton()
        widgets = [
            (cash_address, Address.FMT_CASHADDR),
            (legacy_address, Address.FMT_LEGACY),
            (slp_address, Address.FMT_SLPADDR)
        ]

        def convert_address():
            try:
                addr = Address.from_string(source_address.text().strip())
            except:
                addr = None
            for widget, fmt in widgets:
                if addr:
                    widget.setText(addr.to_full_string(fmt))
                else:
                    widget.setText('')

        source_address.textChanged.connect(convert_address)

        w = QWidget()
        grid = QGridLayout()
        grid.setSpacing(15)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 1)

        label = QLabel(_('&Address to convert'))
        label.setBuddy(source_address)
        grid.addWidget(label, 0, 0)
        grid.addWidget(source_address, 0, 1)

        label = QLabel(_('&Cash address'))
        label.setBuddy(cash_address)
        grid.addWidget(label, 1, 0)
        grid.addWidget(cash_address, 1, 1)

        label = QLabel(_('&Legacy address'))
        label.setBuddy(legacy_address)
        grid.addWidget(label, 2, 0)
        grid.addWidget(legacy_address, 2, 1)
        grid.addWidget(QLabel(_('SLP address')), 3, 0)
        grid.addWidget(slp_address, 3, 1)
        w.setLayout(grid)

        label = WWLabel(_(
            "This tool helps convert between address formats for Bitcoin "
            "Cash addresses.\nYou are encouraged to use the 'Cash address' "
            "format."
        ))

        vbox = QVBoxLayout()
        vbox.addWidget(label)
        vbox.addWidget(w)
        vbox.addStretch(1)

        w = QWidget()
        w.setLayout(vbox)

        return w

    def create_list_tab(self, l, list_header=None):
        class ListTab(QWidget):
            def showEvent(self, e):
                super().showEvent(e)
                if self.main_window.is_slp_wallet:
                    self.main_window.toggle_cashaddr(2, True)
                else:
                    self.main_window.toggle_cashaddr(1, True)

        w = ListTab()
        w.main_window = self
        w.searchable_list = l
        vbox = QVBoxLayout()
        w.setLayout(vbox)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        if list_header:
            hbox = QHBoxLayout()
            for b in list_header:
                hbox.addWidget(b)
            hbox.addStretch()
            vbox.addLayout(hbox)
        vbox.addWidget(l)
        return w

    def create_addresses_tab(self):
        from .address_list import AddressList
        self.address_list = l = AddressList(self)
        self.cashaddr_toggled_signal.connect(l.update)
        return self.create_list_tab(l)

    def create_utxo_tab(self):
        from .utxo_list import UTXOList
        self.utxo_list = l = UTXOList(self)
        self.cashaddr_toggled_signal.connect(l.update)
        return self.create_list_tab(l)

    def create_slp_mgt_tab(self):
        self.create_token_dialog = None
        from .slp_mgt import SlpMgt
        self.token_list = l = SlpMgt(self)
        w = self.create_list_tab(l)
        vbox = w.layout()
        vbox.setSpacing(10)
        create_button = b = QPushButton(_("Create New Token"))
        create_button.setAutoDefault(False)
        create_button.setDefault(False)
        b.clicked.connect(self.show_create_token_dialog)
        vbox.addWidget(create_button)
        w.setLayout(vbox)
        return w

    def show_create_token_dialog(self):
        c, u, x = self.wallet.get_balance()
        bal = c + u - self.wallet.get_slp_locked_balance()
        if bal < 1000:
            self.receive_tab.low_balance_warning_shown = True
            self.show_warning("Low BCH balance.\n\nBefore creating a new token you must add Bitcoin Cash to this wallet.  We recommend a minimum of 0.0001 BCH to get started.\n\nSend BCH to the address displayed in the 'Receive' tab.")
            self.show_receive_tab()
            self.toggle_cashaddr(1, True)
            return
        try:
            self.create_token_dialog.show()
            self.create_token_dialog.raise_()
            self.create_token_dialog.activateWindow()
        except AttributeError:
            self.create_token_dialog = d = SlpCreateTokenGenesisDialog(self,)

    def create_contacts_tab(self):
        from .contact_list import ContactList
        self.contact_list = l = ContactList(self)
        self.cashaddr_toggled_signal.connect(l.update)
        return self.create_list_tab(l)

    def remove_address(self, addr):
        if self.question(_("Do you want to remove {} from your wallet?"
                           .format(addr.to_ui_string()))):
            self.wallet.delete_address(addr)
            self.update_tabs()
            self.update_status()
            self.clear_receive_tab()

    def get_coins(self, isInvoice = False):
        if self.pay_from:
            return self.pay_from
        else:
            return self.wallet.get_spendable_coins(None, self.config, isInvoice)

    def get_slp_coins(self, isInvoice = False):
        return self.wallet.get_slp_spendable_coins(self.slp_token_id, None, self.config, isInvoice)

    def spend_coins(self, coins):
        self.set_pay_from(coins)
        self.show_send_tab()
        self.update_fee()

    def paytomany(self):
        self.show_send_tab()
        self.do_clear()
        self.payto_e.paytomany()
        msg = '\n'.join([
            _('Enter a list of outputs in the \'Pay to\' field.'),
            _('One output per line.'),
            _('Format: address, amount'),
            _('You may load a CSV file using the file icon.')
        ])
        self.show_message(msg, title=_('Pay to many'))

    def payto_contacts(self, labels):
        paytos = [self.get_contact_payto(label) for label in labels]
        self.show_send_tab()
        if len(paytos) == 1:
            self.payto_e.setText(paytos[0])
            self.amount_e.setFocus()
        else:
            text = "\n".join([payto + ", 0" for payto in paytos])
            self.payto_e.setText(text)
            self.payto_e.setFocus()

    def set_contact(self, label, address):
        if not Address.is_valid(address):
            self.show_error(_('Invalid Address'))
            self.contact_list.update()  # Displays original unchanged value
            return False
        old_entry = self.contacts.get(address, None)
        self.contacts[address] = ('address', label)
        self.contact_list.update()
        self.history_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history
        self.update_completions()

        # The contact has changed, update any addresses that are displayed with the old information.
        run_hook('update_contact', address, self.contacts[address], old_entry)
        return True

    def delete_contacts(self, addresses):
        contact_str = " + ".join(addresses) if len(addresses) <= 3 else _("{} contacts").format(len(addresses))
        if not self.question(_("Remove {} from your list of contacts?")
                             .format(contact_str)):
            return
        removed_entries = []
        for address in addresses:
            if address in self.contacts.keys():
                removed_entries.append((address, self.contacts[address]))
            self.contacts.pop(address)

        self.history_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history
        self.contact_list.update()
        self.update_completions()
        run_hook('delete_contacts', removed_entries)

    def add_token_type(self, token_class, token_id, token_name, decimals_divisibility, *, error_callback=None, show_errors=True, allow_overwrite=False):
        if error_callback is None:
            error_callback = self.show_error

        token_name = token_name.strip()

        # Check for duplication error
        d = self.wallet.token_types.get(token_id)
        if not (d is None or allow_overwrite):
            if show_errors:
                error_callback(_('Token with this hash id exists already'))
            return False
        for tid, d in self.wallet.token_types.items():
            if d['name'] == token_name and tid != token_id:
                token_name = token_name + "-" + token_id[:3]
                break

        #Hash id validation
        hexregex='^[a-fA-F0-9]+$'
        gothex=re.match(hexregex,token_id)
        if gothex is None or len(token_id) is not 64:
            if show_errors:
                error_callback(_('Invalid Hash_Id'))
            return False

        #token name validation
        if len(token_name) < 1 or len(token_name)> 20:
            if show_errors:
                error_callback(_('Token name should be 1-20 characters'))
            return False


        new_entry=dict({'class':token_class,'name':token_name,'decimals':decimals_divisibility})

        self.wallet.add_token_type(token_id, new_entry)

        self.token_list.update()
        self.update_token_type_combo()
        self.slp_history_list.update()
        self.wallet.save_transactions(True)
        return True

    def delete_slp_token(self, token_ids):
        if not self.question(_("Remove {} from your list of tokens?")
                             .format(" + ".join(token_ids))):
            return

        for tid in token_ids:
            self.wallet.token_types.pop(tid)

        self.token_list.update()
        self.update_token_type_combo()
        self.slp_history_list.update()
        self.wallet.save_transactions(True)

    def show_invoice(self, key):
        pr = self.invoices.get(key)
        pr.verify(self.contacts)
        self.show_pr_details(pr)

    def show_pr_details(self, pr):
        key = pr.get_id()
        d = WindowModalDialog(self.top_level_window(), _("Invoice"))
        vbox = QVBoxLayout(d)
        grid = QGridLayout()
        grid.addWidget(QLabel(_("Requestor") + ':'), 0, 0)
        grid.addWidget(QLabel(pr.get_requestor()), 0, 1)
        grid.addWidget(QLabel(_("Amount") + ':'), 1, 0)
        outputs_str = '\n'.join(map(lambda x: self.format_amount(x[2])+ self.base_unit() + ' @ ' + x[1].to_ui_string(), pr.get_outputs()))
        grid.addWidget(QLabel(outputs_str), 1, 1)
        expires = pr.get_expiration_date()
        grid.addWidget(QLabel(_("Memo") + ':'), 2, 0)
        grid.addWidget(QLabel(pr.get_memo()), 2, 1)
        grid.addWidget(QLabel(_("Signature") + ':'), 3, 0)
        grid.addWidget(QLabel(pr.get_verify_status()), 3, 1)
        if expires:
            grid.addWidget(QLabel(_("Expires") + ':'), 4, 0)
            grid.addWidget(QLabel(format_time(expires)), 4, 1)
        vbox.addLayout(grid)
        weakD = Weak.ref(d)
        def do_export():
            fn = self.getSaveFileName(_("Save invoice to file"), "*.bip70")
            if not fn:
                return
            with open(fn, 'wb') as f:
                data = f.write(pr.raw)
            self.show_message(_('Invoice saved as' + ' ' + fn))
        exportButton = EnterButton(_('Save'), do_export)
        def do_delete():
            if self.question(_('Delete invoice?')):
                self.invoices.remove(key)
                self.history_list.update()
                self.history_updated_signal.emit() # inform things like address_dialog that there's a new history
                self.invoice_list.update()
                d = weakD()
                if d: d.close()
        deleteButton = EnterButton(_('Delete'), do_delete)
        vbox.addLayout(Buttons(exportButton, deleteButton, CloseButton(d)))
        d.exec_()
        d.setParent(None) # So Python can GC

    def do_pay_invoice(self, key):
        pr = self.invoices.get(key)
        self.payment_request = pr
        self.prepare_for_payment_request()
        pr.error = None  # this forces verify() to re-run
        if pr.verify(self.contacts):
            self.payment_request_ok()
        else:
            self.payment_request_error()

    def create_console_tab(self):
        from .console import Console
        self.console = console = Console(wallet=self.wallet)
        return console

    def update_console(self):
        console = self.console
        console.history = self.config.get("console-history",[])
        console.history_index = len(console.history)

        console.updateNamespace({'wallet' : self.wallet,
                                 'network' : self.network,
                                 'plugins' : self.gui_object.plugins,
                                 'window': self})
        console.updateNamespace({'util' : util, 'bitcoin':bitcoin})

        set_json = Weak(self.console.set_json)
        c = commands.Commands(self.config, self.wallet, self.network, lambda: set_json(True))
        methods = {}
        password_getter = Weak(self.password_dialog)
        def mkfunc(f, method):
            return lambda *args, **kwargs: f(method, *args, password_getter=password_getter,
                                             **kwargs)
        for m in dir(c):
            if m[0]=='_' or m in ['network','wallet','config']: continue
            methods[m] = mkfunc(c._run, m)

        console.updateNamespace(methods)

    def create_status_bar(self):

        sb = QStatusBar()
        sb.setFixedHeight(35)
        qtVersion = qVersion()

        self.balance_label = QLabel("")
        sb.addWidget(self.balance_label)

        self.addr_format_label = QLabel("")
        sb.addPermanentWidget(self.addr_format_label)

        self.search_box = QLineEdit()
        self.search_box.textChanged.connect(self.do_search)
        self.search_box.hide()
        sb.addPermanentWidget(self.search_box)

        self.update_available_button = StatusBarButton(QIcon(":icons/electron-cash-update.svg"), _("Update available, click for details"), lambda: self.gui_object.show_update_checker(self, skip_check=True))
        self.update_available_button.setStatusTip(_("An Electron Cash update is available"))
        sb.addPermanentWidget(self.update_available_button)
        self.update_available_button.setVisible(bool(self.gui_object.new_version_available))  # if hidden now gets unhidden by on_update_available when a new version comes in

        self.lock_icon = QIcon()
        self.password_button = StatusBarButton(self.lock_icon, _("Password"), self.change_password_dialog )
        sb.addPermanentWidget(self.password_button)

        self.addr_converter_button = StatusBarButton(
            self.cashaddr_icon(),
            _("Toggle CashAddr Display"),
            self.toggle_cashaddr_status_bar
        )
        sb.addPermanentWidget(self.addr_converter_button)

        sb.addPermanentWidget(StatusBarButton(QIcon(":icons/preferences.svg"), _("Preferences"), self.settings_dialog ) )
        self.seed_button = StatusBarButton(QIcon(":icons/seed.png"), _("Seed"), self.show_seed_dialog )
        sb.addPermanentWidget(self.seed_button)
        weakSelf = Weak(self)
        gui_object = self.gui_object
        self.status_button = StatusBarButton(QIcon(":icons/status_disconnected.svg"), _("Network"), lambda: gui_object.show_network_dialog(weakSelf))
        sb.addPermanentWidget(self.status_button)
        run_hook('create_status_bar', sb)
        self.setStatusBar(sb)

    def on_update_available(self, b):
        self.update_available_button.setVisible(bool(b))

        # The popup label won't really be shown unless this window is
        # on top.. but regardless we give each label a unique internal name
        # so they dont interfere with each other.
        lblName = "UpdateAvailable_" + self.diagnostic_name()

        if b:
            ShowPopupLabel(name = lblName,
                           text="<center><b>{}</b><br><small>{}</small></center>".format(_("Update Available"),_("Click for details")),
                           target=self.update_available_button,
                           timeout=20000, onClick=self.update_available_button.click,
                           onRightClick=self.update_available_button.click,
                           dark_mode = ColorScheme.dark_scheme)
        else:
            # Immediately kills any extant labels
            KillPopupLabel(lblName)

    def update_lock_icon(self):
        icon = QIcon(":icons/lock.svg") if self.wallet.has_password() else QIcon(":icons/unlock.svg")
        tip = _('Wallet Password') + ' - '
        tip +=  _('Enabled') if self.wallet.has_password() else _('Disabled')
        self.password_button.setIcon(icon)
        self.password_button.setStatusTip(tip)

    def update_buttons_on_seed(self):
        self.seed_button.setVisible(self.wallet.has_seed())
        self.password_button.setVisible(self.wallet.can_change_password())
        is_cointext = bool(self.payto_e.cointext)
        if is_cointext and self.slp_token_id:
            self.token_type_combo.setCurrentIndex(0)
        self.send_button.setVisible(not self.wallet.is_watching_only() and not is_cointext)
        self.preview_button.setVisible(not is_cointext)
        self.cointext_button.setVisible(is_cointext)

    def change_password_dialog(self):
        from .password_dialog import ChangePasswordDialog
        d = ChangePasswordDialog(self.top_level_window(), self.wallet)
        ok, password, new_password, encrypt_file = d.run()
        if not ok:
            return
        try:
            self.wallet.update_password(password, new_password, encrypt_file)
        except BaseException as e:
            self.show_error(str(e))
            return
        except:
            traceback.print_exc(file=sys.stdout)
            self.show_error(_('Failed to update password'))
            return
        msg = _('Password was updated successfully') if new_password else _('Password is disabled, this wallet is not protected')
        self.show_message(msg, title=_("Success"))
        self.update_lock_icon()

    def toggle_search(self):
        self.search_box.setHidden(not self.search_box.isHidden())
        if not self.search_box.isHidden():
            self.search_box.setFocus(1)
        else:
            self.do_search('')

    def do_search(self, t):
        tab = self.tabs.currentWidget()
        if hasattr(tab, 'searchable_list'):
            tab.searchable_list.filter(t)

    def new_contact_dialog(self):
        d = WindowModalDialog(self.top_level_window(), _("New Contact"))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_('New Contact') + ':'))
        grid = QGridLayout()
        line1 = QLineEdit()
        line1.setFixedWidth(280)
        line2 = QLineEdit()
        line2.setFixedWidth(280)
        grid.addWidget(QLabel(_("Address")), 1, 0)
        grid.addWidget(line1, 1, 1)
        grid.addWidget(QLabel(_("Name")), 2, 0)
        grid.addWidget(line2, 2, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if d.exec_():
            self.set_contact(line2.text(), line1.text())

    def show_master_public_keys(self):
        dialog = WindowModalDialog(self.top_level_window(), _("Wallet Information"))
        dialog.setMinimumSize(500, 100)
        mpk_list = self.wallet.get_master_public_keys()
        vbox = QVBoxLayout()
        wallet_type = self.wallet.storage.get('wallet_type', '')
        grid = QGridLayout()
        basename = os.path.basename(self.wallet.storage.path)
        grid.addWidget(QLabel(_("Wallet name")+ ':'), 0, 0)
        grid.addWidget(QLabel(basename), 0, 1)
        grid.addWidget(QLabel(_("Wallet type")+ ':'), 1, 0)
        grid.addWidget(QLabel(wallet_type), 1, 1)
        grid.addWidget(QLabel(_("Script type")+ ':'), 2, 0)
        grid.addWidget(QLabel(self.wallet.txin_type), 2, 1)
        vbox.addLayout(grid)
        if self.wallet.is_deterministic():
            mpk_text = ShowQRTextEdit()
            mpk_text.setMaximumHeight(150)
            mpk_text.addCopyButton()
            def show_mpk(index):
                mpk_text.setText(mpk_list[index])
            # only show the combobox in case multiple accounts are available
            if len(mpk_list) > 1:
                def label(key):
                    if isinstance(self.wallet, Multisig_Wallet):
                        return _("cosigner") + ' ' + str(key+1)
                    return ''
                labels = [label(i) for i in range(len(mpk_list))]
                on_click = lambda clayout: show_mpk(clayout.selected_index())
                labels_clayout = ChoicesLayout(_("Master Public Keys"), labels, on_click)
                vbox.addLayout(labels_clayout.layout())
            else:
                vbox.addWidget(QLabel(_("Master Public Key")))
            show_mpk(0)
            vbox.addWidget(mpk_text)
        vbox.addStretch(1)
        vbox.addLayout(Buttons(CloseButton(dialog)))
        dialog.setLayout(vbox)
        dialog.exec_()

    def remove_wallet(self):
        if self.question('\n'.join([
                _('Delete wallet file?'),
                "%s"%self.wallet.storage.path,
                _('If your wallet contains funds, make sure you have saved its seed.')])):
            self._delete_wallet()

    @protected
    def _delete_wallet(self, password):
        wallet_path = self.wallet.storage.path
        basename = os.path.basename(wallet_path)
        r = self.gui_object.daemon.delete_wallet(wallet_path)  # implicitly also calls stop_wallet
        self.update_recently_visited(wallet_path) # this ensures it's deleted from the menu
        if r:
            self.show_error(_("Wallet removed: {}").format(basename))
        else:
            self.show_error(_("Wallet file not found: {}").format(basename))
        self.close()

    @protected
    def show_seed_dialog(self, password):
        if not self.wallet.has_seed():
            self.show_message(_('This wallet has no seed'))
            return
        keystore = self.wallet.get_keystore()
        try:
            seed = keystore.get_seed(password)
            passphrase = keystore.get_passphrase(password)
        except BaseException as e:
            self.show_error(str(e))
            return
        from .seed_dialog import SeedDialog, SeedBackupDialog
        WhichClass = SeedBackupDialog if self.wallet.storage.get('wallet_seed_needs_backup') else SeedDialog
        d = WhichClass(self.top_level_window(), seed, passphrase, wallet=self.wallet)
        if d.exec_() == QDialog.Accepted:
            # This banch is in case they were in the SeedBackupDialog; below
            # makes the new non-warning icon (if any) take effect
            self.update_status()
        d.setParent(None)  # gc now rather than later

    def show_qrcode(self, data, title = _("QR code"), parent=None):
        if not data:
            return
        d = QRDialog(data, parent or self, title)
        d.exec_()
        d.setParent(None)  # Help Python GC this sooner rather than later

    @protected
    def show_private_key(self, address, password):
        if not address:
            return
        try:
            pk = self.wallet.export_private_key(address, password)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return
        xtype = bitcoin.deserialize_privkey(pk)[0]
        d = WindowModalDialog(self.top_level_window(), _("Private key"))
        d.setMinimumSize(600, 150)
        vbox = QVBoxLayout()
        vbox.addWidget(QLabel('{}: {}'.format(_("Address"), address)))
        vbox.addWidget(QLabel(_("Script type") + ': ' + xtype))
        vbox.addWidget(QLabel(_("Private key") + ':'))
        keys_e = ShowQRTextEdit(text=pk)
        keys_e.addCopyButton()
        vbox.addWidget(keys_e)
        vbox.addWidget(QLabel(_("Redeem Script") + ':'))
        rds_e = ShowQRTextEdit(text=address.to_script().hex())
        rds_e.addCopyButton()
        vbox.addWidget(rds_e)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setLayout(vbox)
        d.exec_()

    msg_sign = _("Signing with an address actually means signing with the corresponding "
                "private key, and verifying with the corresponding public key. The "
                "address you have entered does not have a unique public key, so these "
                "operations cannot be performed.") + '\n\n' + \
               _('The operation is undefined. Not just in Electron Cash, but in general.')

    @protected
    def do_sign(self, address, message, signature, password):
        address  = address.text().strip()
        message = message.toPlainText().strip()
        try:
            addr = Address.from_string(address)
        except:
            self.show_message(_('Invalid Bitcoin Cash address.'))
            return
        if addr.kind != addr.ADDR_P2PKH:
            self.show_message(_('Cannot sign messages with this type of address.') + '\n\n' + self.msg_sign)
        if self.wallet.is_watching_only():
            self.show_message(_('This is a watching-only wallet.'))
            return
        if not self.wallet.is_mine(addr):
            self.show_message(_('Address not in wallet.'))
            return
        task = partial(self.wallet.sign_message, addr, message, password)

        def show_signed_message(sig):
            signature.setText(base64.b64encode(sig).decode('ascii'))
        self.wallet.thread.add(task, on_success=show_signed_message)

    def do_verify(self, address, message, signature):
        try:
            address = Address.from_string(address.text().strip())
        except:
            self.show_message(_('Invalid Bitcoin Cash address.'))
            return
        message = message.toPlainText().strip().encode('utf-8')
        try:
            # This can throw on invalid base64
            sig = base64.b64decode(signature.toPlainText())
            verified = bitcoin.verify_message(address, sig, message)
        except:
            verified = False

        if verified:
            self.show_message(_("Signature verified"))
        else:
            self.show_error(_("Wrong signature"))

    def sign_verify_message(self, address=None):
        d = WindowModalDialog(self.top_level_window(), _('Sign/verify Message'))
        d.setMinimumSize(610, 290)

        layout = QGridLayout(d)

        message_e = QTextEdit()
        message_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Message')), 1, 0)
        layout.addWidget(message_e, 1, 1)
        layout.setRowStretch(2,3)

        address_e = QLineEdit()
        address_e.setText(address.to_ui_string() if address else '')
        layout.addWidget(QLabel(_('Address')), 2, 0)
        layout.addWidget(address_e, 2, 1)

        signature_e = QTextEdit()
        signature_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Signature')), 3, 0)
        layout.addWidget(signature_e, 3, 1)
        layout.setRowStretch(3,1)

        hbox = QHBoxLayout()

        b = QPushButton(_("Sign"))
        b.clicked.connect(lambda: self.do_sign(address_e, message_e, signature_e))
        hbox.addWidget(b)

        b = QPushButton(_("Verify"))
        b.clicked.connect(lambda: self.do_verify(address_e, message_e, signature_e))
        hbox.addWidget(b)

        b = QPushButton(_("Close"))
        b.clicked.connect(d.accept)
        hbox.addWidget(b)
        layout.addLayout(hbox, 4, 1)
        d.exec_()

    @protected
    def do_decrypt(self, message_e, pubkey_e, encrypted_e, password):
        if self.wallet.is_watching_only():
            self.show_message(_('This is a watching-only wallet.'))
            return
        cyphertext = encrypted_e.toPlainText()
        task = partial(self.wallet.decrypt_message, pubkey_e.text(), cyphertext, password)
        self.wallet.thread.add(task, on_success=lambda text: message_e.setText(text.decode('utf-8')))

    def do_encrypt(self, message_e, pubkey_e, encrypted_e):
        message = message_e.toPlainText()
        message = message.encode('utf-8')
        try:
            encrypted = bitcoin.encrypt_message(message, pubkey_e.text())
            encrypted_e.setText(encrypted.decode('ascii'))
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_warning(str(e))

    def encrypt_message(self, address=None):
        d = WindowModalDialog(self.top_level_window(), _('Encrypt/decrypt Message'))
        d.setMinimumSize(610, 490)

        layout = QGridLayout(d)

        message_e = QTextEdit()
        message_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Message')), 1, 0)
        layout.addWidget(message_e, 1, 1)
        layout.setRowStretch(2,3)

        pubkey_e = QLineEdit()
        if address:
            pubkey = self.wallet.get_public_key(address)
            if not isinstance(pubkey, str):
                pubkey = pubkey.to_ui_string()
            pubkey_e.setText(pubkey)
        layout.addWidget(QLabel(_('Public key')), 2, 0)
        layout.addWidget(pubkey_e, 2, 1)

        encrypted_e = QTextEdit()
        encrypted_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Encrypted')), 3, 0)
        layout.addWidget(encrypted_e, 3, 1)
        layout.setRowStretch(3,1)

        hbox = QHBoxLayout()
        b = QPushButton(_("Encrypt"))
        b.clicked.connect(lambda: self.do_encrypt(message_e, pubkey_e, encrypted_e))
        hbox.addWidget(b)

        b = QPushButton(_("Decrypt"))
        b.clicked.connect(lambda: self.do_decrypt(message_e, pubkey_e, encrypted_e))
        hbox.addWidget(b)

        b = QPushButton(_("Close"))
        b.clicked.connect(d.accept)
        hbox.addWidget(b)
        layout.addLayout(hbox, 4, 1)
        d.exec_()

    def password_dialog(self, msg=None, parent=None):
        from .password_dialog import PasswordDialog
        parent = parent or self
        return PasswordDialog(parent, msg).run()

    def tx_from_text(self, txt):
        from electroncash.transaction import tx_from_str
        try:
            txt_tx = tx_from_str(txt)
            tx = Transaction(txt_tx, sign_schnorr=self.wallet.is_schnorr_enabled())
            tx.deserialize()
            if self.wallet:
                my_coins = self.wallet.get_spendable_coins(None, self.config)
                my_outpoints = [vin['prevout_hash'] + ':' + str(vin['prevout_n']) for vin in my_coins]
                for i, txin in enumerate(tx.inputs()):
                    outpoint = txin['prevout_hash'] + ':' + str(txin['prevout_n'])
                    if outpoint in my_outpoints:
                        my_index = my_outpoints.index(outpoint)
                        tx._inputs[i]['value'] = my_coins[my_index]['value']
            return tx
        except:
            traceback.print_exc(file=sys.stdout)
            self.show_critical(_("Electron Cash was unable to parse your transaction"))
            return

    # Due to the asynchronous nature of the qr reader we need to keep the
    # dialog instance as member variable to prevent reentrancy/multiple ones
    # from being presented at once.
    _qr_dialog = None

    def read_tx_from_qrcode(self):
        if self._qr_dialog:
            # Re-entrancy prevention -- there is some lag between when the user
            # taps the QR button and the modal dialog appears.  We want to
            # prevent multiple instances of the dialog from appearing, so we
            # must do this.
            self.print_error("Warning: QR dialog is already presented, ignoring.")
            return
        if self.gui_object.warn_if_cant_import_qrreader(self):
            return
        from electroncash import get_config
        from .qrreader import QrReaderCameraDialog
        data = ''
        self._qr_dialog = None
        try:
            self._qr_dialog = QrReaderCameraDialog(parent=self.top_level_window())

            def _on_qr_reader_finished(success: bool, error: str, result):
                if self._qr_dialog:
                    self._qr_dialog.deleteLater(); self._qr_dialog = None
                if not success:
                    if error:
                        self.show_error(error)
                    return
                if not result:
                    return
                # if the user scanned a bitcoincash URI
                if result.lower().startswith(networks.net.CASHADDR_PREFIX + ':') or result.lower().startswith(networks.net.SLPADDR_PREFIX + ':'):
                    self.pay_to_URI(result)
                    return
                # else if the user scanned an offline signed tx
                try:
                    result = bh2u(bitcoin.base_decode(result, length=None, base=43))
                    tx = self.tx_from_text(result)  # will show an error dialog on error
                    if not tx:
                        return
                except BaseException as e:
                    self.show_error(str(e))
                    return
                self.show_transaction(tx)

            self._qr_dialog.qr_finished.connect(_on_qr_reader_finished)
            self._qr_dialog.start_scan(get_config().get_video_device())
        except BaseException as e:
            if util.is_verbose:
                import traceback
                traceback.print_exc()
            self._qr_dialog = None
            self.show_error(str(e))

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

    def do_process_from_text(self):
        from electroncash.transaction import SerializationError
        text = text_dialog(self.top_level_window(), _('Input raw transaction'), _("Transaction:"), _("Load transaction"))
        if not text:
            return
        try:
            tx = self.tx_from_text(text)
            if tx:
                self.show_transaction(tx)
        except SerializationError as e:
            self.show_critical(_("Electron Cash was unable to deserialize the transaction:") + "\n" + str(e))

    def do_process_from_file(self, *, fileName = None):
        from electroncash.transaction import SerializationError
        try:
            tx = self.read_tx_from_file(fileName=fileName)
            if tx:
                self.show_transaction(tx)
        except SerializationError as e:
            self.show_critical(_("Electron Cash was unable to deserialize the transaction:") + "\n" + str(e))

    def do_process_from_txid(self, *, txid=None, parent=None):
        parent = parent or self
        if self.gui_object.warn_if_no_network(parent):
            return
        from electroncash import transaction
        ok = txid is not None
        if not ok:
            txid, ok = QInputDialog.getText(parent, _('Lookup transaction'), _('Transaction ID') + ':')
        if ok and txid:
            ok, r = self.network.get_raw_tx_for_txid(txid, timeout=10.0)
            if not ok:
                parent.show_message(_("Error retrieving transaction") + ":\n" + r)
                return
            tx = transaction.Transaction(r, sign_schnorr=self.wallet.is_schnorr_enabled())  # note that presumably the tx is already signed if it comes from blockchain so this sign_schnorr parameter is superfluous, but here to satisfy my OCD -Calin
            self.show_transaction(tx)

    @protected
    def export_privkeys_dialog(self, password):
        if self.wallet.is_watching_only():
            self.show_message(_("This is a watching-only wallet"))
            return

        if isinstance(self.wallet, Multisig_Wallet):
            self.show_message(_('WARNING: This is a multi-signature wallet.') + '\n' +
                              _('It can not be "backed up" by simply exporting these private keys.'))

        d = WindowModalDialog(self.top_level_window(), _('Private keys'))
        d.setMinimumSize(850, 300)
        vbox = QVBoxLayout(d)

        msg = "%s\n%s\n%s" % (_("WARNING: ALL your private keys are secret."),
                              _("Exposing a single private key can compromise your entire wallet!"),
                              _("In particular, DO NOT use 'redeem private key' services proposed by third parties."))
        vbox.addWidget(QLabel(msg))

        e = QTextEdit()
        e.setReadOnly(True)
        vbox.addWidget(e)

        defaultname = 'electron-cash-private-keys.csv'
        select_msg = _('Select file to export your private keys to')
        hbox, filename_e, csv_button = filename_field(self, self.config, defaultname, select_msg)
        vbox.addLayout(hbox)

        b = OkButton(d, _('Export'))
        b.setEnabled(False)
        vbox.addLayout(Buttons(CancelButton(d), b))

        private_keys = {}
        addresses = self.wallet.get_addresses()
        done = False
        cancelled = False
        def privkeys_thread():
            for addr in addresses:
                time.sleep(0.1)
                if done or cancelled:
                    break
                try:
                    privkey = self.wallet.export_private_key(addr, password)
                except InvalidPassword:
                    # See #921 -- possibly a corrupted wallet or other strangeness
                    privkey = 'INVALID_PASSWORD'
                private_keys[addr.to_ui_string()] = privkey
                self.computing_privkeys_signal.emit()
            if not cancelled:
                self.computing_privkeys_signal.disconnect()
                self.show_privkeys_signal.emit()

        def show_privkeys():
            s = "\n".join('{}\t{}'.format(addr, privkey)
                          for addr, privkey in private_keys.items())
            e.setText(s)
            b.setEnabled(True)
            self.show_privkeys_signal.disconnect()
            nonlocal done
            done = True

        def on_dialog_closed(*args):
            nonlocal done
            nonlocal cancelled
            if not done:
                cancelled = True
                self.computing_privkeys_signal.disconnect()
                self.show_privkeys_signal.disconnect()

        self.computing_privkeys_signal.connect(lambda: e.setText(_("Please wait... {num}/{total}").format(num=len(private_keys),total=len(addresses))))
        self.show_privkeys_signal.connect(show_privkeys)
        d.finished.connect(on_dialog_closed)
        threading.Thread(target=privkeys_thread).start()

        res = d.exec_()
        d.setParent(None) # for python GC
        if not res:
            done = True
            return

        filename = filename_e.text()
        if not filename:
            return

        try:
            self.do_export_privkeys(filename, private_keys, csv_button.isChecked())
        except (IOError, os.error) as reason:
            txt = "\n".join([
                _("Electron Cash was unable to produce a private key-export."),
                str(reason)
            ])
            self.show_critical(txt, title=_("Unable to create csv"))

        except Exception as e:
            self.show_message(str(e))
            return

        self.show_message(_("Private keys exported."))

    def do_export_privkeys(self, fileName, pklist, is_csv):
        with open(fileName, "w+", encoding='utf-8') as f:
            if is_csv:
                transaction = csv.writer(f)
                transaction.writerow(["address", "private_key"])
                for addr, pk in pklist.items():
                    transaction.writerow(["%34s"%addr,pk])
            else:
                f.write(json.dumps(pklist, indent = 4))

    def do_import_labels(self):
        labelsFile = self.getOpenFileName(_("Open labels file"), "*.json")
        if not labelsFile: return
        try:
            with open(labelsFile, 'r', encoding='utf-8') as f:  # always ensure UTF-8. See issue #1453.
                data = f.read()
                data = json.loads(data)
            if type(data) is not dict or not len(data) or not all(type(v) is str and type(k) is str for k,v in data.items()):
                self.show_critical(_("The file you selected does not appear to contain labels."))
                return
            for key, value in data.items():
                self.wallet.set_label(key, value)
            self.show_message(_("Your labels were imported from") + " '%s'" % str(labelsFile))
        except (IOError, OSError, json.decoder.JSONDecodeError) as reason:
            self.show_critical(_("Electron Cash was unable to import your labels.") + "\n" + str(reason))
        self.address_list.update()
        self.history_list.update()
        self.utxo_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history

    def do_export_labels(self):
        labels = self.wallet.labels
        try:
            fileName = self.getSaveFileName(_("Select file to save your labels"), 'electron-cash_labels.json', "*.json")
            if fileName:
                with open(fileName, 'w+', encoding='utf-8') as f:  # always ensure UTF-8. See issue #1453.
                    json.dump(labels, f, indent=4, sort_keys=True)
                self.show_message(_("Your labels were exported to") + " '%s'" % str(fileName))
        except (IOError, os.error) as reason:
            self.show_critical(_("Electron Cash was unable to export your labels.") + "\n" + str(reason))

    def export_history_dialog(self):
        d = WindowModalDialog(self.top_level_window(), _('Export History'))
        d.setMinimumSize(400, 200)
        vbox = QVBoxLayout(d)
        defaultname = os.path.expanduser('~/electron-cash-history.csv')
        select_msg = _('Select file to export your wallet transactions to')
        hbox, filename_e, csv_button = filename_field(self, self.config, defaultname, select_msg)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        hbox = Buttons(CancelButton(d), OkButton(d, _('Export')))
        vbox.addLayout(hbox)
        run_hook('export_history_dialog', self, hbox)
        self.update()
        res = d.exec_()
        d.setParent(None) # for python GC
        if not res:
            return
        filename = filename_e.text()
        if not filename:
            return
        try:
            self.do_export_history(self.wallet, filename, csv_button.isChecked())
        except (IOError, os.error) as reason:
            export_error_label = _("Electron Cash was unable to produce a transaction export.")
            self.show_critical(export_error_label + "\n" + str(reason), title=_("Unable to export history"))
            return
        self.show_message(_("Your wallet history has been successfully exported."))

    def plot_history_dialog(self):
        if plot_history is None:
            return
        wallet = self.wallet
        history = wallet.get_history()
        if len(history) > 0:
            plt = plot_history(self.wallet, history)
            plt.show()

    def do_export_history(self, wallet, fileName, is_csv):
        history = wallet.export_history(fx=self.fx)
        ccy = (self.fx and self.fx.get_currency()) or ''
        has_fiat_columns = history and self.fx and self.fx.show_history() and 'fiat_value' in history[0] and 'fiat_balance' in history[0]
        lines = []
        for item in history:
            if is_csv:
                cols = [item['txid'], item.get('label', ''), item['confirmations'], item['value'], item['date']]
                if has_fiat_columns:
                    cols += [item['fiat_value'], item['fiat_balance']]
                lines.append(cols)
            else:
                if has_fiat_columns and ccy:
                    item['fiat_currency'] = ccy  # add the currency to each entry in the json. this wastes space but json is bloated anyway so this won't hurt too much, we hope
                elif not has_fiat_columns:
                    # No need to include these fields as they will always be 'No Data'
                    item.pop('fiat_value', None)
                    item.pop('fiat_balance', None)
                lines.append(item)

        with open(fileName, "w+", encoding="utf-8") as f:  # ensure encoding to utf-8. Avoid Windows cp1252. See #1453.
            if is_csv:
                transaction = csv.writer(f, lineterminator='\n')
                cols = ["transaction_hash","label", "confirmations", "value", "timestamp"]
                if has_fiat_columns:
                    cols += [f"fiat_value_{ccy}", f"fiat_balance_{ccy}"]  # in CSV mode, we use column names eg fiat_value_USD, etc
                transaction.writerow(cols)
                for line in lines:
                    transaction.writerow(line)
            else:
                f.write(json.dumps(lines, indent=4))

    def sweep_key_dialog(self):
        addresses = self.wallet.get_unused_addresses()
        if not addresses:
            try:
                addresses = self.wallet.get_receiving_addresses()
            except AttributeError:
                addresses = self.wallet.get_addresses()
        if not addresses:
            self.show_warning(_('Wallet has no address to sweep to'))
            return

        d = WindowModalDialog(self.top_level_window(), title=_('Sweep private keys'))
        d.setMinimumSize(600, 300)

        vbox = QVBoxLayout(d)
        bip38_warn_label = QLabel(_("<b>BIP38 support is disabled because a requisite library is not installed.</b> Please install 'cryptodomex' or omit BIP38 private keys (private keys starting in 6P...). Decrypt keys to WIF format (starting with 5, K, or L) in order to sweep."))
        bip38_warn_label.setWordWrap(True)
        bip38_warn_label.setHidden(True)
        vbox.addWidget(bip38_warn_label)
        extra = ""
        if bitcoin.is_bip38_available():
            extra += " " + _('or BIP38 keys')
        vbox.addWidget(QLabel(_("Enter private keys") + extra + " :"))

        keys_e = ScanQRTextEdit(allow_multi=True)
        keys_e.setTabChangesFocus(True)
        vbox.addWidget(keys_e)

        h, addr_combo = address_combo(addresses)
        vbox.addLayout(h)

        vbox.addStretch(1)
        sweep_button = OkButton(d, _('Sweep'))
        vbox.addLayout(Buttons(CancelButton(d), sweep_button))

        def get_address_text():
            return addr_combo.currentText()

        def get_priv_keys():
            return keystore.get_private_keys(keys_e.toPlainText(), allow_bip38=True)

        def has_bip38_keys_but_no_bip38():
            if bitcoin.is_bip38_available():
                return False
            keys = [k for k in keys_e.toPlainText().split() if k]
            return any(bitcoin.is_bip38_key(k) for k in keys)

        def enable_sweep():
            bad_bip38 = has_bip38_keys_but_no_bip38()
            sweepok = bool(get_address_text() and not bad_bip38 and get_priv_keys())
            sweep_button.setEnabled(sweepok)
            bip38_warn_label.setHidden(not bad_bip38)

        keys_e.textChanged.connect(enable_sweep)
        enable_sweep()
        res = d.exec_()
        d.setParent(None)
        if not res:
            return

        try:
            self.do_clear()
            keys = get_priv_keys()
            bip38s = {}
            for i, k in enumerate(keys):
                if bitcoin.is_bip38_key(k):
                    bip38s[k] = i
            if bip38s:
                # For all the BIP38s detected, prompt for password
                from .bip38_importer import Bip38Importer
                d2 = Bip38Importer(bip38s.keys(), parent=self.top_level_window())
                d2.exec_()
                d2.setParent(None)
                if d2.decoded_keys:
                    for k,tup in d2.decoded_keys.items():
                        wif, adr = tup
                        # rewrite the keys they specified with the decrypted WIF in the keys list for sweep_preparations to work below...
                        i = bip38s[k]
                        keys[i] = wif
                else:
                    self.show_message(_("User cancelled"))
                    return
            coins, keypairs = sweep_preparations(keys, self.network)
            self.tx_external_keypairs = keypairs
            self.payto_e.setText(get_address_text())
            self.spend_coins(coins)
            self.spend_max()
        except BaseException as e:
            self.show_message(str(e))
            return
        self.payto_e.setFrozen(True)
        self.amount_e.setFrozen(True)
        self.warn_if_watching_only()

    def _do_import(self, title, msg, func):
        text = text_dialog(self.top_level_window(), title, msg + ' :', _('Import'),
                           allow_multi=True)
        if not text:
            return
        bad, bad_info = [], []
        good = []
        for key in str(text).split():
            try:
                addr = func(key)
                good.append(addr)
            except BaseException as e:
                bad.append(key)
                bad_info.append("{}: {}".format(key, str(e)))
                continue
        if good:
            self.show_message(_("The following addresses were added") + ':\n' + '\n'.join(good))
        if bad:
            self.show_warning(_("The following could not be imported") + ':\n' + '\n'.join(bad), detail_text='\n\n'.join(bad_info))
        self.address_list.update()
        self.history_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history

    def import_addresses(self):
        if not self.wallet.can_import_address():
            return
        title, msg = _('Import addresses'), _("Enter addresses")
        def import_addr(addr):
            if self.wallet.import_address(Address.from_string(addr)):
                return addr
            return ''
        self._do_import(title, msg, import_addr)

    @protected
    def do_import_privkey(self, password):
        if not self.wallet.can_import_privkey():
            return
        title, msg = _('Import private keys'), _("Enter private keys")
        if bitcoin.is_bip38_available():
            msg += " " + _('or BIP38 keys')
        def func(key):
            if bitcoin.is_bip38_available() and bitcoin.is_bip38_key(key):
                from .bip38_importer import Bip38Importer
                d = Bip38Importer([key], parent=self.top_level_window(),
                    message = _('A BIP38 key was specified, please enter a password to decrypt it'),
                    show_count = False)
                d.exec_()
                d.setParent(None)  # python GC quicker if this happens
                if d.decoded_keys:
                    wif, adr = d.decoded_keys[key]
                    return self.wallet.import_private_key(wif, password)
                else:
                    raise util.UserCancelled()
            else:
                return self.wallet.import_private_key(key, password)
        self._do_import(title, msg, func)

    def update_fiat(self):
        b = self.fx and self.fx.is_enabled()
        if not self.amount_e.isVisible():
            self.fiat_send_e.setVisible(False)
        else:
            self.fiat_send_e.setVisible(b)
        self.fiat_receive_e.setVisible(b)
        self.history_list.refresh_headers()
        self.history_list.update()
        self.history_updated_signal.emit() # inform things like address_dialog that there's a new history
        self.address_list.refresh_headers()
        self.address_list.update()
        self.update_status()

    def cashaddr_icon(self):
        if self.config.get('addr_format', 0) == 1:
            return QIcon(":icons/tab_converter.svg")
        elif self.config.get('addr_format', 0)==2:
            return QIcon(":icons/tab_converter_slp.svg")
        else:
            return QIcon(":icons/tab_converter_bw.svg")

    def update_cashaddr_icon(self):
        self.addr_converter_button.setIcon(self.cashaddr_icon())

    def toggle_cashaddr_status_bar(self):
        self.toggle_cashaddr(self.config.get('addr_format', 2))

    def toggle_cashaddr_settings(self,state):
        self.toggle_cashaddr(state, True)

    def toggle_cashaddr(self, format, specified = False):
        #Gui toggle should just increment, if "specified" is True it is being set from preferences, so leave the value as is.
        if specified==False:
            if self.is_slp_wallet:
                max_format=2
            else:
                max_format=1
            format+=1
            if format > max_format:
                format=0
        self.config.set_key('addr_format', format)
        Address.show_cashaddr(format)
        self.setAddrFormatText(format)
        for window in self.gui_object.windows:
            window.cashaddr_toggled_signal.emit()

    def setAddrFormatText(self, format):
        try:
            if format == 0:
                self.addr_format_label.setText("Addr Format: Legacy")
            elif format == 1:
                self.addr_format_label.setText("Addr Format: Cash")
            else:
                self.addr_format_label.setText("Addr Format: SLP")
        except AttributeError:
            pass

    def settings_dialog(self):
        self.need_restart = False
        d = WindowModalDialog(self.top_level_window(), _('Preferences'))
        vbox = QVBoxLayout()
        tabs = QTabWidget()
        gui_widgets = []
        fee_widgets = []
        global_tx_widgets, per_wallet_tx_widgets = [], []
        id_widgets = []

        addr_format_choices = ["Legacy Format","CashAddr Format","SLP Format"]
        addr_format_dict={'Legacy Format':0,'CashAddr Format':1,'SLP Format':2}
        msg = _('Choose which format the wallet displays for Bitcoin Cash addresses')
        addr_format_label = HelpLabel(_('Address Format') + ':', msg)
        addr_format_combo = QComboBox()
        addr_format_combo.addItems(addr_format_choices)
        addr_format_combo.setCurrentIndex(self.config.get("addr_format", 0))
        addr_format_combo.currentIndexChanged.connect(self.toggle_cashaddr_settings)

        gui_widgets.append((addr_format_label,addr_format_combo))

        # language
        lang_help = _('Select which language is used in the GUI (after restart).')
        lang_label = HelpLabel(_('Language') + ':', lang_help)
        lang_combo = QComboBox()
        from electroncash.i18n import languages, get_system_language_match, match_language

        language_names = []
        language_keys = []
        for (lang_code, lang_def) in languages.items():
            language_keys.append(lang_code)
            lang_name = []
            lang_name.append(lang_def.name)
            if lang_code == '':
                # System entry in languages list (==''), gets system setting
                sys_lang = get_system_language_match()
                if sys_lang:
                    lang_name.append(f' [{languages[sys_lang].name}]')
            language_names.append(''.join(lang_name))
        lang_combo.addItems(language_names)
        conf_lang = self.config.get("language", '')
        if conf_lang:
            # The below code allows us to rename languages in saved config and
            # have them still line up with languages in our languages dict.
            # For example we used to save English as en_UK but now it's en_US
            # and it will still match
            conf_lang = match_language(conf_lang)
        try: index = language_keys.index(conf_lang)
        except ValueError: index = 0
        lang_combo.setCurrentIndex(index)

        if not self.config.is_modifiable('language'):
            for w in [lang_combo, lang_label]:
                w.setEnabled(False)

        def on_lang(x):
            lang_request = language_keys[lang_combo.currentIndex()]
            if lang_request != self.config.get('language'):
                self.config.set_key("language", lang_request, True)
                self.need_restart = True
        lang_combo.currentIndexChanged.connect(on_lang)
        gui_widgets.append((lang_label, lang_combo))

        nz_help = _('Number of zeros displayed after the decimal point. For example, if this is set to 2, "1." will be displayed as "1.00"')
        nz_label = HelpLabel(_('Zeros after decimal point') + ':', nz_help)
        nz = QSpinBox()
        nz.setMinimum(0)
        nz.setMaximum(self.decimal_point)
        nz.setValue(self.num_zeros)
        if not self.config.is_modifiable('num_zeros'):
            for w in [nz, nz_label]: w.setEnabled(False)
        def on_nz():
            value = nz.value()
            if self.num_zeros != value:
                self.num_zeros = value
                self.config.set_key('num_zeros', value, True)
                self.update_tabs()
                self.update_status()
        nz.valueChanged.connect(on_nz)
        gui_widgets.append((nz_label, nz))

        def on_customfee(x):
            amt = customfee_e.get_amount()
            m = int(amt * 1000.0) if amt is not None else None
            self.config.set_key('customfee', m)
            self.fee_slider.update()
            if self.fee_custom_lbl.text() == '':
                self.fee_slider_mogrifier(self.get_custom_fee_text())
            else:
                self.fee_slider_mogrifier()

        customfee_e = BTCSatsByteEdit()
        customfee_e.setAmount(self.config.custom_fee_rate() / 1000.0 if self.config.has_custom_fee_rate() else None)
        customfee_e.textChanged.connect(on_customfee)
        customfee_label = HelpLabel(_('Custom Fee Rate'), _('Custom Fee Rate in Satoshis per byte'))
        fee_widgets.append((customfee_label, customfee_e))

        feebox_cb = QCheckBox(_('Edit fees manually'))
        feebox_cb.setChecked(self.config.get('show_fee', False))
        feebox_cb.setToolTip(_("Show fee edit box in send tab."))
        def on_feebox(x):
            self.config.set_key('show_fee', x == Qt.Checked)
            self.fee_e.setVisible(bool(x))
        feebox_cb.stateChanged.connect(on_feebox)
        fee_widgets.append((feebox_cb, None))

        msg = _('OpenAlias record, used to receive coins and to sign payment requests.') + '\n\n'\
              + _('The following alias providers are available:') + '\n'\
              + '\n'.join(['https://cryptoname.co/', 'http://xmr.link/']) + '\n\n'\
              + _('For more information, see http://openalias.org')
        alias_label = HelpLabel(_('OpenAlias') + ':', msg)
        alias = self.config.get('alias','')
        alias_e = QLineEdit(alias)
        def set_alias_color():
            if not self.config.get('alias'):
                alias_e.setStyleSheet("")
                return
            if self.alias_info:
                alias_addr, alias_name, validated = self.alias_info
                alias_e.setStyleSheet((ColorScheme.GREEN if validated else ColorScheme.RED).as_stylesheet(True))
            else:
                alias_e.setStyleSheet(ColorScheme.RED.as_stylesheet(True))
        def on_alias_edit():
            alias_e.setStyleSheet("")
            alias = str(alias_e.text())
            self.config.set_key('alias', alias, True)
            if alias:
                self.fetch_alias()
        set_alias_color()
        self.alias_received_signal.connect(set_alias_color)
        alias_e.editingFinished.connect(on_alias_edit)
        id_widgets.append((alias_label, alias_e))

        # SSL certificate
        msg = ' '.join([
            _('SSL certificate used to sign payment requests.'),
            _('Use setconfig to set ssl_chain and ssl_privkey.'),
        ])
        if self.config.get('ssl_privkey') or self.config.get('ssl_chain'):
            try:
                SSL_identity = paymentrequest.check_ssl_config(self.config)
                SSL_error = None
            except BaseException as e:
                SSL_identity = "error"
                SSL_error = str(e)
        else:
            SSL_identity = ""
            SSL_error = None
        SSL_id_label = HelpLabel(_('SSL certificate') + ':', msg)
        SSL_id_e = QLineEdit(SSL_identity)
        SSL_id_e.setStyleSheet((ColorScheme.RED if SSL_error else ColorScheme.GREEN).as_stylesheet(True) if SSL_identity else '')
        if SSL_error:
            SSL_id_e.setToolTip(SSL_error)
        SSL_id_e.setReadOnly(True)
        id_widgets.append((SSL_id_label, SSL_id_e))

        units = util.base_unit_labels  # ( 'BCH', 'mBCH', 'bits' )
        msg = _('Base unit of your wallet.')\
              + '\n1 BCH = 1,000 mBCH = 1,000,000 bits.\n' \
              + _(' These settings affects the fields in the Send tab')+' '
        unit_label = HelpLabel(_('Base unit') + ':', msg)
        unit_combo = QComboBox()
        unit_combo.addItems(units)
        unit_combo.setCurrentIndex(units.index(self.base_unit()))
        def on_unit(x, nz):
            unit_result = units[unit_combo.currentIndex()]
            if self.base_unit() == unit_result:
                return
            edits = self.amount_e, self.fee_e, self.receive_amount_e
            amounts = [edit.get_amount() for edit in edits]
            dp = util.base_units.get(unit_result)
            if dp is not None:
                self.decimal_point = dp
            else:
                raise Exception('Unknown base unit')
            self.config.set_key('decimal_point', self.decimal_point, True)
            nz.setMaximum(self.decimal_point)
            for edit, amount in zip(edits, amounts):
                edit.setAmount(amount)
            self.update_tabs()
            self.update_status()
        unit_combo.currentIndexChanged.connect(lambda x: on_unit(x, nz))
        gui_widgets.append((unit_label, unit_combo))



        block_explorers = web.BE_sorted_list()
        msg = _('Choose which online block explorer to use for functions that open a web browser')
        block_ex_label = HelpLabel(_('Online Block Explorer') + ':', msg)
        block_ex_combo = QComboBox()
        block_ex_combo.addItems(block_explorers)
        block_ex_combo.setCurrentIndex(block_ex_combo.findText(web.BE_from_config(self.config)))
        def on_be(x):
            be_result = block_explorers[block_ex_combo.currentIndex()]
            self.config.set_key('block_explorer', be_result, True)
        block_ex_combo.currentIndexChanged.connect(on_be)
        gui_widgets.append((block_ex_label, block_ex_combo))

        qr_combo = QComboBox()
        qr_combo.addItem(_("Default"),"default")
        system_cameras = []
        try:
            from PyQt5.QtMultimedia import QCameraInfo
            system_cameras = QCameraInfo.availableCameras()
            qr_label = HelpLabel(_('Video Device') + ':', _("For scanning Qr codes."))
        except ImportError as e:
            # Older Qt or missing libs -- disable GUI control and inform user why
            qr_combo.setEnabled(False)
            qr_combo.setToolTip(_("Unable to probe for cameras on this system. QtMultimedia is likely missing."))
            qr_label = HelpLabel(_('Video Device') + ' ' + _('(disabled)') + ':', qr_combo.toolTip() + "\n\n" + str(e))
            qr_label.setToolTip(qr_combo.toolTip())
        for cam in system_cameras:
            qr_combo.addItem(cam.description(), cam.deviceName())
        video_device = self.config.get("video_device")
        video_device_index = 0
        if video_device:
            video_device_index = qr_combo.findData(video_device)
        qr_combo.setCurrentIndex(video_device_index)
        on_video_device = lambda x: self.config.set_key("video_device", qr_combo.itemData(x), True)
        qr_combo.currentIndexChanged.connect(on_video_device)
        gui_widgets.append((qr_label, qr_combo))

        colortheme_combo = QComboBox()
        colortheme_combo.addItem(_('Light'), 'default')
        colortheme_combo.addItem(_('Dark'), 'dark')
        theme_name = self.config.get('qt_gui_color_theme', 'default')
        dark_theme_available = self.gui_object.is_dark_theme_available()
        if theme_name == 'dark' and not dark_theme_available:
            theme_name = 'default'
        index = colortheme_combo.findData(theme_name)
        if index < 0: index = 0
        colortheme_combo.setCurrentIndex(index)
        msg = ( _("Dark theme support requires the package 'QDarkStyle' (typically installed via the 'pip3' command on Unix & macOS).")
               if not dark_theme_available
               else '' )
        lbltxt = _('Color theme') + ':'
        colortheme_label = HelpLabel(lbltxt, msg) if msg else QLabel(lbltxt)
        def on_colortheme(x):
            item_data = colortheme_combo.itemData(x)
            if not dark_theme_available and item_data == 'dark':
                self.show_error(_("Dark theme is not available. Please install QDarkStyle to access this feature."))
                colortheme_combo.setCurrentIndex(0)
                return
            self.config.set_key('qt_gui_color_theme', item_data, True)
            if theme_name != item_data:
                self.need_restart = True
        colortheme_combo.currentIndexChanged.connect(on_colortheme)
        gui_widgets.append((colortheme_label, colortheme_combo))

        if sys.platform not in ('darwin',):
            # Enable/Disable HighDPI -- this option makes no sense for macOS
            # and thus does not appear on that platform
            hidpi_chk = QCheckBox(_('Automatic high DPI scaling'))
            if sys.platform in ('linux',):
                hidpi_chk.setToolTip(_("Enable/disable this option if you experience graphical glitches (such as overly large status bar icons)"))
            else: # windows
                hidpi_chk.setToolTip(_("Enable/disable this option if you experience graphical glitches (such as dialog box text being cut off"))
            hidpi_chk.setChecked(bool(self.config.get('qt_enable_highdpi', True)))
            if self.config.get('qt_disable_highdpi'):
                hidpi_chk.setToolTip(_('Automatic high DPI scaling was disabled from the command-line'))
                hidpi_chk.setChecked(False)
                hidpi_chk.setDisabled(True)
            def on_hi_dpi_toggle():
                self.config.set_key('qt_enable_highdpi', hidpi_chk.isChecked())
                self.need_restart = True
            hidpi_chk.stateChanged.connect(on_hi_dpi_toggle)
            gui_widgets.append((hidpi_chk, None))

        gui_widgets.append((None, None)) # spacer
        updatecheck_cb = QCheckBox(_("Automatically check for updates"))
        updatecheck_cb.setChecked(self.gui_object.has_auto_update_check())
        updatecheck_cb.setToolTip(_("Enable this option if you wish to be notified as soon as a new version of Electron Cash becomes available"))
        def on_set_updatecheck(v):
            self.gui_object.set_auto_update_check(v == Qt.Checked)
        updatecheck_cb.stateChanged.connect(on_set_updatecheck)
        gui_widgets.append((updatecheck_cb, None))


        notify_tx_cb = QCheckBox(_('Notify when receiving funds'))
        notify_tx_cb.setToolTip(_('If enabled, a system notification will be presented when you receive funds to this wallet.'))
        notify_tx_cb.setChecked(bool(self.wallet.storage.get('gui_notify_tx', True)))
        def on_notify_tx(b):
            self.wallet.storage.put('gui_notify_tx', bool(b))
        notify_tx_cb.stateChanged.connect(on_notify_tx)
        per_wallet_tx_widgets.append((notify_tx_cb, None))


        usechange_cb = QCheckBox(_('Use change addresses'))
        if self.force_use_single_change_addr:
            usechange_cb.setChecked(True)
            usechange_cb.setEnabled(False)
            if isinstance(self.force_use_single_change_addr, str):
                usechange_cb.setToolTip(self.force_use_single_change_addr)
        else:
            usechange_cb.setChecked(self.wallet.use_change)
            usechange_cb.setToolTip(_('Using change addresses makes it more difficult for other people to track your transactions.'))
            def on_usechange(x):
                usechange_result = x == Qt.Checked
                if self.wallet.use_change != usechange_result:
                    self.wallet.use_change = usechange_result
                    self.wallet.storage.put('use_change', self.wallet.use_change)
                    multiple_cb.setEnabled(self.wallet.use_change)
            usechange_cb.stateChanged.connect(on_usechange)
        per_wallet_tx_widgets.append((usechange_cb, None))

        multiple_change = self.wallet.multiple_change
        multiple_cb = QCheckBox(_('Use multiple change addresses'))
        if self.force_use_single_change_addr:
            multiple_cb.setEnabled(False)
            multiple_cb.setChecked(False)
            if isinstance(self.force_use_single_change_addr, str):
                multiple_cb.setToolTip(self.force_use_single_change_addr)
        else:
            multiple_cb.setEnabled(self.wallet.use_change)
            multiple_cb.setToolTip('\n'.join([
                _('In some cases, use up to 3 change addresses in order to break '
                  'up large coin amounts and obfuscate the recipient address.'),
                _('This may result in higher transactions fees.')
            ]))
            multiple_cb.setChecked(multiple_change)
            def on_multiple(x):
                multiple = x == Qt.Checked
                if self.wallet.multiple_change != multiple:
                    self.wallet.multiple_change = multiple
                    self.wallet.storage.put('multiple_change', multiple)
            multiple_cb.stateChanged.connect(on_multiple)
        per_wallet_tx_widgets.append((multiple_cb, None))

        def fmt_docs(key, klass):
            lines = [ln.lstrip(" ") for ln in klass.__doc__.split("\n")]
            return '\n'.join([key, "", " ".join(lines)])

        def on_unconf(x):
            self.config.set_key('confirmed_only', bool(x))
        conf_only = self.config.get('confirmed_only', False)
        unconf_cb = QCheckBox(_('Spend only confirmed coins'))
        unconf_cb.setToolTip(_('Spend only confirmed inputs.'))
        unconf_cb.setChecked(conf_only)
        unconf_cb.stateChanged.connect(on_unconf)
        global_tx_widgets.append((unconf_cb, None))

        # Fiat Currency
        hist_checkbox = QCheckBox()
        fiat_address_checkbox = QCheckBox()
        ccy_combo = QComboBox()
        ex_combo = QComboBox()

        enable_opreturn = bool(self.config.get('enable_opreturn'))
        opret_cb = QCheckBox(_('Enable OP_RETURN output'))
        opret_cb.setToolTip(_('Enable posting messages with OP_RETURN.'))
        opret_cb.setChecked(enable_opreturn)
        opret_cb.stateChanged.connect(self.on_toggled_opreturn)
        global_tx_widgets.append((opret_cb,None))

        # Schnorr
        use_schnorr_cb = QCheckBox(_("Enable Schnorr signatures"))
        use_schnorr_cb.setChecked(self.wallet.is_schnorr_enabled())
        use_schnorr_cb.stateChanged.connect(self.wallet.set_schnorr_enabled)
        no_schnorr_reason = []
        if self.wallet.is_schnorr_possible(no_schnorr_reason):
            use_schnorr_cb.setEnabled(True)
            use_schnorr_cb.setToolTip(_("Sign all transactions using Schnorr signatures."))
        else:
            # not possible (wallet type not supported); show reason in tooltip
            use_schnorr_cb.setEnabled(False)
            use_schnorr_cb.setToolTip(no_schnorr_reason[0])
        per_wallet_tx_widgets.append((use_schnorr_cb, None))

        def update_currencies():
            if not self.fx: return
            currencies = sorted(self.fx.get_currencies(self.fx.get_history_config()))
            ccy_combo.clear()
            ccy_combo.addItems([_('None')] + currencies)
            if self.fx.is_enabled():
                ccy_combo.setCurrentIndex(ccy_combo.findText(self.fx.get_currency()))

        def update_history_cb():
            if not self.fx: return
            hist_checkbox.setChecked(self.fx.get_history_config())
            hist_checkbox.setEnabled(self.fx.is_enabled())

        def update_fiat_address_cb():
            if not self.fx: return
            fiat_address_checkbox.setChecked(self.fx.get_fiat_address_config())

        def update_exchanges():
            if not self.fx: return
            b = self.fx.is_enabled()
            ex_combo.setEnabled(b)
            if b:
                c = self.fx.get_currency()
                h = self.fx.get_history_config()
            else:
                c, h = self.fx.default_currency, False
            exchanges = self.fx.get_exchanges_by_ccy(c, h)
            conf_exchange = self.fx.config_exchange()
            ex_combo.clear()
            ex_combo.addItems(sorted(exchanges))
            idx = ex_combo.findText(conf_exchange)  # try and restore previous exchange if in new list
            if idx < 0:
                # hmm, previous exchange wasn't in new h= setting. Try default exchange.
                idx = ex_combo.findText(self.fx.default_exchange)
            idx = 0 if idx < 0 else idx # if still no success (idx < 0) -> default to the first exchange in combo
            if exchanges: # don't set index if no exchanges, as any index is illegal. this shouldn't happen.
                ex_combo.setCurrentIndex(idx)  # note this will emit a currentIndexChanged signal if it's changed


        def on_currency(hh):
            if not self.fx: return
            b = bool(ccy_combo.currentIndex())
            ccy = str(ccy_combo.currentText()) if b else None
            self.fx.set_enabled(b)
            if b and ccy != self.fx.ccy:
                self.fx.set_currency(ccy)
            update_history_cb()
            update_exchanges()
            self.update_fiat()

        def on_exchange(idx):
            exchange = str(ex_combo.currentText())
            if self.fx and self.fx.is_enabled() and exchange and exchange != self.fx.exchange.name():
                self.fx.set_exchange(exchange)

        def on_history(checked):
            if not self.fx: return
            changed = bool(self.fx.get_history_config()) != bool(checked)
            self.fx.set_history_config(checked)
            update_exchanges()
            self.history_list.refresh_headers()
            self.slp_history_list.refresh_headers()
            if self.fx.is_enabled() and checked:
                # reset timeout to get historical rates
                self.fx.timeout = 0
                if changed:
                    self.history_list.update()  # this won't happen too often as it's rate-limited

        def on_fiat_address(checked):
            if not self.fx: return
            self.fx.set_fiat_address_config(checked)
            self.address_list.refresh_headers()
            self.address_list.update()

        update_currencies()
        update_history_cb()
        update_fiat_address_cb()
        update_exchanges()
        ccy_combo.currentIndexChanged.connect(on_currency)
        hist_checkbox.stateChanged.connect(on_history)
        fiat_address_checkbox.stateChanged.connect(on_fiat_address)
        ex_combo.currentIndexChanged.connect(on_exchange)

        fiat_widgets = []
        fiat_widgets.append((QLabel(_('Fiat currency')), ccy_combo))
        fiat_widgets.append((QLabel(_('Show history rates')), hist_checkbox))
        fiat_widgets.append((QLabel(_('Show Fiat balance for addresses')), fiat_address_checkbox))
        fiat_widgets.append((QLabel(_('Source')), ex_combo))

        tabs_info = [
            (gui_widgets, _('General')),
            (fee_widgets, _('Fees')),
            (OrderedDict([
                ( _("App-Global Options") , global_tx_widgets ),
                ( _("Per-Wallet Options") , per_wallet_tx_widgets),
             ]), _('Transactions')),
            (fiat_widgets, _('Fiat')),
            (id_widgets, _('Identity')),
        ]
        def add_tabs_info_to_tabs(tabs, tabs_info):
            def add_widget_pair(a,b,grid):
                i = grid.rowCount()
                if b:
                    if a:
                        grid.addWidget(a, i, 0)
                    grid.addWidget(b, i, 1)
                else:
                    if a:
                        grid.addWidget(a, i, 0, 1, 2)
                    else:
                        grid.addItem(QSpacerItem(15, 15), i, 0, 1, 2)
            for thing, name in tabs_info:
                tab = QWidget()
                if isinstance(thing, dict):
                    # This Prefs tab is laid out as groupboxes one atop another...
                    d = thing
                    vbox = QVBoxLayout(tab)
                    for groupName, widgets in d.items():
                        gbox = QGroupBox(groupName)
                        grid = QGridLayout(gbox)
                        grid.setColumnStretch(0,1)
                        for a,b in widgets:
                            add_widget_pair(a,b,grid)
                        vbox.addWidget(gbox, len(widgets))
                else:
                    # Standard layout.. 1 tab has just a grid of widgets
                    widgets = thing
                    grid = QGridLayout(tab)
                    grid.setColumnStretch(0,1)
                    for a,b in widgets:
                        add_widget_pair(a,b,grid)
                tabs.addTab(tab, name)
        # / add_tabs_info_to_tabs
        add_tabs_info_to_tabs(tabs, tabs_info)

        vbox.addWidget(tabs)
        vbox.addStretch(1)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setLayout(vbox)

        # run the dialog
        d.exec_()
        d.setParent(None) # for Python GC

        if self.fx:
            self.fx.timeout = 0

        self.alias_received_signal.disconnect(set_alias_color)

        run_hook('close_settings_dialog')
        if self.need_restart:
            self.show_warning(_('Please restart Electron Cash to activate the new GUI settings'), title=_('Success'))

    def closeEvent(self, event):
        # It seems in some rare cases this closeEvent() is called twice
        if not self.cleaned_up:
            self.cleaned_up = True
            self.clean_up()
        event.accept()

    def is_alive(self): return bool(not self.cleaned_up)

    def clean_up_connections(self):
        def disconnect_signals():
            for attr_name in dir(self):
                if attr_name.endswith("_signal"):
                    sig = getattr(self, attr_name)
                    if isinstance(sig, pyqtBoundSignal):
                        try: sig.disconnect()
                        except TypeError: pass # no connections
                elif attr_name.endswith("__RateLimiter"): # <--- NB: this needs to match the attribute name in util.py rate_limited decorator
                    rl_obj = getattr(self, attr_name)
                    if isinstance(rl_obj, RateLimiter):
                        rl_obj.kill_timer()
            try: self.disconnect()
            except TypeError: pass
        def disconnect_network_callbacks():
            if self.network:
                self.network.unregister_callback(self.on_network)
                self.network.unregister_callback(self.on_quotes)
                self.network.unregister_callback(self.on_history)
        # /
        disconnect_network_callbacks()
        disconnect_signals()

    def clean_up_children(self):
        # status bar holds references to self, so clear it to help GC this window
        # Note that due to quirks on macOS and the shared menu bar, we do *NOT* clear
        # the menuBar.  But I've found it goes away anyway on its own after window deletion.
        self.setStatusBar(None)
        # Reparent children to 'None' so python GC can clean them up sooner rather than later.
        # This also hopefully helps accelerate this window's GC.
        children = [c for c in self.children()
                    if (isinstance(c, (QWidget,QAction,QShortcut,TaskThread))
                        and not isinstance(c, (QStatusBar, QMenuBar, QFocusFrame)))]
        for c in children:
            try: c.disconnect()
            except TypeError: pass
            c.setParent(None)

    def clean_up(self):
        self.wallet.thread.stop()
        self.wallet.thread.wait() # Join the thread to make sure it's really dead.

        # We catch these errors with the understanding that there is no recovery at
        # this point, given user has likely performed an action we cannot recover
        # cleanly from.  So we attempt to exit as cleanly as possible.
        try:
            self.config.set_key("is_maximized", self.isMaximized())
            self.config.set_key("console-history", self.console.history[-50:], True)
        except (OSError, PermissionError) as e:
            self.print_error("unable to write to config (directory removed?)", e)

        if not self.isMaximized():
            try:
                g = self.geometry()
                self.wallet.storage.put("winpos-qt", [g.left(),g.top(),g.width(),g.height()])
            except (OSError, PermissionError) as e:
                self.print_error("unable to write to wallet storage (directory removed?)", e)

        # Should be no side-effects in this function relating to file access past this point.
        if self.qr_window:
            self.qr_window.close()
            self.qr_window = None # force GC sooner rather than later.
        for d in list(self._tx_dialogs):
            # clean up all extant tx dialogs we opened as they hold references
            # to us that will be invalidated
            d.prompt_if_unsaved = False  # make sure to unconditionally close
            d.close()
        self._close_wallet()


        try: self.gui_object.timer.timeout.disconnect(self.timer_actions)
        except TypeError: pass # defensive programming: this can happen if we got an exception before the timer action was connected

        self.gui_object.close_window(self) # implicitly runs the hook: on_close_window
        # Now, actually STOP the wallet's synchronizer and verifiers and remove
        # it from the daemon. Note that its addresses will still stay
        # 'subscribed' to the ElectrumX server until we connect to a new server,
        # (due to ElectrumX protocol limitations).. but this is harmless.
        self.gui_object.daemon.stop_wallet(self.wallet.storage.path)

        # At this point all plugins should have removed any references to this window.
        # Now, just to be paranoid, do some active destruction of signal/slot connections as well as
        # Removing child widgets forcefully to speed up Python's own GC of this window.
        self.clean_up_connections()
        self.clean_up_children()

        # And finally, print when we are destroyed by C++ for debug purposes
        # We must call this here as above calls disconnected all signals
        # involving this widget.
        destroyed_print_error(self)


    def internal_plugins_dialog(self):
        if self.internalpluginsdialog:
            # NB: reentrance here is possible due to the way the window menus work on MacOS.. so guard against it
            self.internalpluginsdialog.raise_()
            return
        d = WindowModalDialog(self.top_level_window(), _('Optional Features'))
        weakD = Weak.ref(d)

        gui_object = self.gui_object
        plugins = gui_object.plugins

        vbox = QVBoxLayout(d)

        # plugins
        scroll = QScrollArea()
        scroll.setEnabled(True)
        scroll.setWidgetResizable(True)
        scroll.setMinimumSize(400,250)
        vbox.addWidget(scroll)

        w = QWidget()
        scroll.setWidget(w)
        w.setMinimumHeight(plugins.get_internal_plugin_count() * 35)

        grid = QGridLayout()
        grid.setColumnStretch(0,1)
        weakGrid = Weak.ref(grid)
        w.setLayout(grid)

        settings_widgets = Weak.ValueDictionary()

        def enable_settings_widget(p, name, i):
            widget = settings_widgets.get(name)
            grid = weakGrid()
            d = weakD()
            if d and grid and not widget and p and p.requires_settings():
                widget = settings_widgets[name] = p.settings_widget(d)
                grid.addWidget(widget, i, 1)
            if widget:
                widget.setEnabled(bool(p and p.is_enabled()))
                if not p:
                    # Need to delete settings widget because keeping it around causes bugs as it points to a now-dead plugin instance
                    settings_widgets.pop(name)
                    widget.hide(); widget.setParent(None); widget.deleteLater(); widget = None

        def do_toggle(weakCb, name, i):
            cb = weakCb()
            if cb:
                p = plugins.toggle_internal_plugin(name)
                cb.setChecked(bool(p))
                enable_settings_widget(p, name, i)
                # All plugins get this whenever one is toggled.
                run_hook('init_qt', gui_object)

        for i, descr in enumerate(plugins.internal_plugin_metadata.values()):
            name = descr['__name__']
            p = plugins.get_internal_plugin(name)
            if descr.get('registers_keystore'):
                continue
            try:
                cb = QCheckBox(descr['fullname'])
                weakCb = Weak.ref(cb)
                plugin_is_loaded = p is not None
                cb_enabled = (not plugin_is_loaded and plugins.is_internal_plugin_available(name, self.wallet)
                              or plugin_is_loaded and p.can_user_disable())
                cb.setEnabled(cb_enabled)
                cb.setChecked(plugin_is_loaded and p.is_enabled())
                grid.addWidget(cb, i, 0)
                enable_settings_widget(p, name, i)
                cb.clicked.connect(partial(do_toggle, weakCb, name, i))
                msg = descr['description']
                if descr.get('requires'):
                    msg += '\n\n' + _('Requires') + ':\n' + '\n'.join(map(lambda x: x[1], descr.get('requires')))
                grid.addWidget(HelpButton(msg), i, 2)
            except Exception:
                self.print_msg("error: cannot display plugin", name)
                traceback.print_exc(file=sys.stdout)
        grid.setRowStretch(len(plugins.internal_plugin_metadata.values()), 1)
        vbox.addLayout(Buttons(CloseButton(d)))
        self.internalpluginsdialog = d
        d.exec_()
        self.internalpluginsdialog = None # Python GC please!

    def external_plugins_dialog(self):
        if self.externalpluginsdialog:
            # NB: reentrance here is possible due to the way the window menus work on MacOS.. so guard against it
            self.externalpluginsdialog.raise_()
            return
        from . import external_plugins_window
        d = external_plugins_window.ExternalPluginsDialog(self, _('Plugin Manager'))
        self.externalpluginsdialog = d
        d.exec_()
        self.externalpluginsdialog = None # allow python to GC

    def cpfp(self, parent_tx, new_tx):
        total_size = parent_tx.estimated_size() + new_tx.estimated_size()
        d = WindowModalDialog(self.top_level_window(), _('Child Pays for Parent'))
        vbox = QVBoxLayout(d)
        msg = (
            "A CPFP is a transaction that sends an unconfirmed output back to "
            "yourself, with a high fee. The goal is to have miners confirm "
            "the parent transaction in order to get the fee attached to the "
            "child transaction.")
        vbox.addWidget(WWLabel(_(msg)))
        msg2 = ("The proposed fee is computed using your "
            "fee/kB settings, applied to the total size of both child and "
            "parent transactions. After you broadcast a CPFP transaction, "
            "it is normal to see a new unconfirmed transaction in your history.")
        vbox.addWidget(WWLabel(_(msg2)))
        grid = QGridLayout()
        grid.addWidget(QLabel(_('Total size') + ':'), 0, 0)
        grid.addWidget(QLabel('%d bytes'% total_size), 0, 1)
        max_fee = new_tx.output_value()
        grid.addWidget(QLabel(_('Input amount') + ':'), 1, 0)
        grid.addWidget(QLabel(self.format_amount(max_fee) + ' ' + self.base_unit()), 1, 1)
        output_amount = QLabel('')
        grid.addWidget(QLabel(_('Output amount') + ':'), 2, 0)
        grid.addWidget(output_amount, 2, 1)
        fee_e = BTCAmountEdit(self.get_decimal_point)
        def f(x):
            a = max_fee - fee_e.get_amount()
            output_amount.setText((self.format_amount(a) + ' ' + self.base_unit()) if a else '')
        fee_e.textChanged.connect(f)
        fee = self.config.fee_per_kb() * total_size / 1000
        fee_e.setAmount(fee)
        grid.addWidget(QLabel(_('Fee' + ':')), 3, 0)
        grid.addWidget(fee_e, 3, 1)
        def on_rate(dyn, pos, fee_rate):
            fee = fee_rate * total_size / 1000
            fee = min(max_fee, fee)
            fee_e.setAmount(fee)
        fee_slider = FeeSlider(self, self.config, on_rate)
        fee_slider.update()
        grid.addWidget(fee_slider, 4, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        result = d.exec_()
        d.setParent(None) # So Python can GC
        if not result:
            return
        fee = fee_e.get_amount()
        if fee > max_fee:
            self.show_error(_('Max fee exceeded'))
            return
        new_tx = self.wallet.cpfp(parent_tx, fee)
        if new_tx is None:
            self.show_error(_('CPFP no longer valid'))
            return
        self.show_transaction(new_tx)

    def rebuild_history(self):
        if self.gui_object.warn_if_no_network(self):
            # Don't allow if offline mode.
            return
        msg = ' '.join([
            _('This feature is intended to allow you to rebuild a wallet if it has become corrupted.'),
            "\n\n"+_('Your entire transaction history will be downloaded again from the server and verified from the blockchain.'),
            _('Just to be safe, back up your wallet file first!'),
            "\n\n"+_("Rebuild this wallet's history now?")
        ])
        if self.question(msg, title=_("Rebuild Wallet History")):
            try:
                self.wallet.rebuild_history()
            except RuntimeError as e:
                self.show_error(str(e))

    def scan_beyond_gap(self):
        if self.gui_object.warn_if_no_network(self):
            return
        from .scan_beyond_gap import ScanBeyondGap
        d = ScanBeyondGap(self)
        d.exec_()
        d.setParent(None)  # help along Python by dropping refct to 0

    def copy_to_clipboard(self, text, tooltip=None, widget=None):
        tooltip = tooltip or _("Text copied to clipboard")
        widget = widget or self
        qApp.clipboard().setText(text)
        QToolTip.showText(QCursor.pos(), tooltip, widget)


class TxUpdateMgr(QObject, PrintError):
    ''' Manages new transaction notifications and transaction verified
    notifications from the network thread. It collates them and sends them to
    the appropriate GUI controls in the main_window in an efficient manner. '''
    def __init__(self, main_window_parent):
        assert isinstance(main_window_parent, ElectrumWindow), "TxUpdateMgr must be constructed with an ElectrumWindow as its parent"
        super().__init__(main_window_parent)
        self.lock = threading.Lock()  # used to lock thread-shared attrs below
        # begin thread-shared attributes
        self.notif_q = []
        self.verif_q = []
        self.need_process_v, self.need_process_n = False, False
        # /end thread-shared attributes
        self.weakParent = Weak.ref(main_window_parent)
        main_window_parent.history_updated_signal.connect(self.verifs_get_and_clear, Qt.DirectConnection)  # immediately clear verif_q on history update because it would be redundant to keep the verify queue around after a history list update
        main_window_parent.on_timer_signal.connect(self.do_check, Qt.DirectConnection)  # hook into main_window's timer_actions function

    def diagnostic_name(self):
        return ((self.weakParent() and self.weakParent().diagnostic_name()) or "???") + "." + __class__.__name__

    def do_check(self):
        ''' Called from timer_actions in main_window to check if notifs or
        verifs need to update the GUI.
          - Checks the need_process_[v|n] flags
          - If either flag is set, call the @rate_limited process_verifs
            and/or process_notifs functions which update GUI parent in a
            rate-limited (collated) fashion (for decent GUI responsiveness). '''
        with self.lock:
            bV, bN = self.need_process_v, self.need_process_n
            self.need_process_v, self.need_process_n = False, False
        if bV: self.process_verifs()  # rate_limited call (1 per second)
        if bN: self.process_notifs()  # rate_limited call (1 per 15 seconds)

    def verifs_get_and_clear(self):
        ''' Clears the verif_q. This is called from the network
        thread for the 'verified2' event as well as from the below
        update_verifs (GUI thread), hence the lock. '''
        with self.lock:
            ret = self.verif_q
            self.verif_q = []
            self.need_process_v = False
            return ret

    def notifs_get_and_clear(self):
        with self.lock:
            ret = self.notif_q
            self.notif_q = []
            self.need_process_n = False
            return ret

    def verif_add(self, args):
        # args: [wallet, tx_hash, height, conf, timestamp]
        # filter out tx's not for this wallet
        parent = self.weakParent()
        if not parent or parent.cleaned_up:
            return
        if args[0] is parent.wallet:
            with self.lock:
                self.verif_q.append(args[1:])
                self.need_process_v = True

    def notif_add(self, args):
        parent = self.weakParent()
        if not parent or parent.cleaned_up:
            return
        tx, wallet = args
        # filter out tx's not for this wallet
        if wallet is parent.wallet:
            with self.lock:
                self.notif_q.append(tx)
                self.need_process_n = True

    @rate_limited(1.0, ts_after=True)
    def process_verifs(self):
        ''' Update history list with tx's from verifs_q, but limit the
        GUI update rate to once per second. '''
        parent = self.weakParent()
        if not parent or parent.cleaned_up:
            return
        items = self.verifs_get_and_clear()
        if items:
            parent.history_list.setUpdatesEnabled(False)
            parent.slp_history_list.setUpdatesEnabled(False)
            n_updates = 0
            for item in items:
                did_update = parent.history_list.update_item(*item)
                parent.slp_history_list.update_item_netupdate(*item)
                n_updates += 1 if did_update else 0
            self.print_error("Updated {}/{} verified txs in GUI"
                             .format(n_updates, len(items)))
            parent.slp_history_list.setUpdatesEnabled(True)
            parent.history_list.setUpdatesEnabled(True)
            parent.update_status()

    @rate_limited(5.0, classlevel=True)
    def process_notifs(self):
        parent = self.weakParent()
        if not parent or parent.cleaned_up:
            return
        if parent.network:
            n_ok = 0
            txns = self.notifs_get_and_clear()
            if txns and parent.wallet.storage.get('gui_notify_tx', True):
                # Combine the transactions
                total_amount = 0
                tokens_included = set()
                for tx in txns:
                    if tx:
                        is_relevant, is_mine, v, fee = parent.wallet.get_wallet_delta(tx)
                        if is_relevant:
                            total_amount += v
                            n_ok += 1
                        if "slp_" in parent.wallet.storage.get('wallet_type', ''):
                            try:
                                tti = parent.wallet.get_slp_token_info(tx.txid())
                                tokens_included.add(parent.wallet.token_types.get(tti['token_id'],{}).get('name','unknown'))
                            except KeyError:
                                pass
                if tokens_included:
                    tokstring = _('. Tokens included: ') + ', '.join(sorted(tokens_included))
                else:
                    tokstring = ''
                if total_amount > 0:
                    self.print_error("Notifying GUI %d tx"%(n_ok))
                    if n_ok > 1:
                        parent.notify(_("{} new transactions: {}{}")
                                    .format(n_ok, parent.format_amount_and_units(total_amount, is_diff=True), tokstring))
                    else:
                        parent.notify(_("New transaction: {}{}").format(parent.format_amount_and_units(total_amount, is_diff=True), tokstring))
