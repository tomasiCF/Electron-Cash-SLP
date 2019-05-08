#!/usr/bin/env python3
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2013 ecdsa@github
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

from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from electroncash.i18n import _

from .util import *
from .qrtextedit import ShowQRTextEdit, ScanQRTextEdit


def seed_warning_msg(seed):
    return ''.join([
        "<p>",
        _("Please save these %d words on paper (order is important). "),
        _("This seed will allow you to recover your wallet in case "
          "of computer failure."),
        "</p>",
        "<b>" + _("WARNING") + ":</b>",
        "<ul>",
        "<li>" + _("Never disclose your seed.") + "</li>",
        "<li>" + _("Never type it on a website.") + "</li>",
        "<li>" + _("Do not store it electronically.") + "</li>",
        "<li>" + _("Do not use this seed on a non-SLP wallet as it may result in lost tokens.") + "</li>",
        "</ul>"
    ]) % len(seed.split())


class SeedLayout(QVBoxLayout):
    #options
    is_bip39 = False
    is_ext = False
    is_bip39_145 = False

    def seed_options(self):
        dialog = QDialog()
        vbox = QVBoxLayout(dialog)
        if 'ext' in self.options:
            cb_ext = QCheckBox(_('Extend this seed with custom words') + " " + _("(aka 'passphrase')"))
            cb_ext.setChecked(self.is_ext)
            vbox.addWidget(cb_ext)
        '''
        if 'bip39' in self.options:  # SLP hack -- never allow user to uncheck this
            def f(b):
                self.is_seed = (lambda x: bool(x)) if b else self.saved_is_seed
                self.is_bip39 = b
                self.on_edit()
                if b:
                    msg = ' '.join([
                        '<b>' + _('Warning') + ':</b>  ',
                        _('BIP39 seeds can be imported in Electron Cash, so that users can access funds locked in other wallets.'),
                        _('However, we do not generate BIP39 seeds, because they do not meet our safety standard.'),
                        _('BIP39 seeds do not include a version number, which compromises compatibility with future software.'),
                        _('We do not guarantee that BIP39 imports will always be supported in Electron Cash.'),
                    ])
                else:
                    msg = ''
                self.seed_warning.setText(msg)
            #cb_bip39 = QCheckBox(_('BIP39 seed'))
            #cb_bip39.toggled.connect(f)
            #cb_bip39.setChecked(self.is_bip39)
            #vbox.addWidget(cb_bip39)



        if 'bip39_145' in self.options:  # hard coded off for SLP
            def f(b):
                self.is_seed = (lambda x: bool(x)) if b else self.saved_is_seed
                self.on_edit()
                self.is_bip39 = b
                if b:
                    msg = ' '.join([
                        '<b>' + _('Warning') + ': BIP39 seeds are dangerous!' + '</b><br/><br/>',
                        _('BIP39 seeds can be imported in Electron Cash so that users can access funds locked in other wallets.'),
                        _('However, BIP39 seeds do not include a version number, which compromises compatibility with future wallet software.'),
                        '<br/><br/>',
                        _('We do not guarantee that BIP39 imports will always be supported in Electron Cash.'),
                        _('In addition, Electron Cash does not verify the checksum of BIP39 seeds; make sure you type your seed correctly.'),
                    ])
                else:
                    msg = ''
                self.seed_warning.setText(msg)
            cb_bip39_145 = QCheckBox(_('Use Coin Type 145 with bip39'))
            cb_bip39_145.toggled.connect(f)
            cb_bip39_145.setChecked(self.is_bip39_145)
            vbox.addWidget(cb_bip39_145)

        '''
        vbox.addLayout(Buttons(OkButton(dialog)))
        dialog.setWindowModality(Qt.WindowModal)
        if not dialog.exec_():
            return None
        self.is_ext = cb_ext.isChecked() if 'ext' in self.options else False
        self.is_bip39 = True #Hard coded for SLP #cb_bip39.isChecked() if 'bip39' in self.options else False
        self.is_bip39_145 = False #cb_bip39_145.isChecked() if 'bip39_145' in self.options else False

    def __init__(self, seed=None, title=None, icon=True, msg=None, options=None, is_seed=None, passphrase=None, parent=None, editable=True,
                 can_skip=None):
        QVBoxLayout.__init__(self)
        self.parent = parent
        self.options = options
        self.is_bip39 = True  # Hard-coded for SLP
        self.is_bip39_145 = False # Hard-coded for SLP
        self.is_seed = is_seed = lambda x: bool(x) # Hard-coded for SLP
        self.was_skipped = False
        if title:
            self.addWidget(WWLabel(title))
        self.seed_e = ButtonsTextEdit()
        self.seed_e.setReadOnly(not editable)
        if seed:
            self.seed_e.setText(seed)
        else:
            self.seed_e.setTabChangesFocus(True)
            self.is_seed = is_seed
            self.saved_is_seed = self.is_seed
            self.seed_e.textChanged.connect(self.on_edit)
        self.seed_e.setMaximumHeight(75)
        hbox = QHBoxLayout()
        if icon:
            logo = QLabel()
            logo.setPixmap(QPixmap(":icons/seed.png").scaledToWidth(64))
            logo.setMaximumWidth(60)
            hbox.addWidget(logo)
        hbox.addWidget(self.seed_e)
        self.addLayout(hbox)
        hbox = QHBoxLayout()
        hbox.addStretch(1)
        self.seed_type_label = QLabel('')
        hbox.addWidget(self.seed_type_label)
        if options:
            opt_button = EnterButton(_('Options'), self.seed_options)
            hbox.addWidget(opt_button)
            self.addLayout(hbox)
        if can_skip:
            skip_button = EnterButton(_('Skip this step'), self.on_skip_button)
            hbox.addWidget(skip_button)
            self.addLayout(hbox)
        if passphrase:
            hbox = QHBoxLayout()
            passphrase_e = QLineEdit()
            passphrase_e.setText(passphrase)
            passphrase_e.setReadOnly(True)
            hbox.addWidget(QLabel(_("Your seed extension is") + ':'))
            hbox.addWidget(passphrase_e)
            self.addLayout(hbox)
        self.addStretch(1)
        self.seed_warning = WWLabel('')
        if msg:
            self.seed_warning.setText(seed_warning_msg(seed))
        self.addWidget(self.seed_warning)

    def get_seed(self):
        text = self.seed_e.text()
        return ' '.join(text.split())

    @staticmethod
    def _slp_custom_chk(s, is_seed):
        from electroncash.bitcoin import seed_type
        from electroncash.keystore import bip39_is_checksum_valid
        is_checksum, is_wordlist = bip39_is_checksum_valid(s)
        if not is_seed:
            return '', 'no seed', False, False, False
        if not is_wordlist:
            return '', 'unknown wordlist', is_checksum, is_wordlist, False
        else:
            if is_checksum:
                return 'BIP39', 'checksum: ok', is_checksum, is_wordlist, False
            else:
                try:
                    st = seed_type(s)
                    if st in ('old', 'standard'):
                        return 'Electron Cash regular seed', 'not SLP', is_checksum, is_wordlist, True
                except:
                    # seed_type may raise i think
                    pass
                return 'BIP39', 'checksum: failed', is_checksum, is_wordlist, False

    def on_edit(self):
        # NOTE: this has been heavily modified for SLP -- it completely
        # does not support non-BIP39 seeds (Electron Cash standard + old seeds)
        # When merging SLP into mainline in the future -- this function
        # will need to be resurrected with the original Electron Cash logic
        s = self.get_seed()
        b = self.is_seed(s)  # this is just a test for non-empty string on SLP
        label, status, is_checksum, is_wordlist, is_electrum_seed = self._slp_custom_chk(s, b)
        label_text = label + (' ' if label else '') + ('(%s)'%status)
        self.seed_type_label.setText(label_text)
        self.parent.next_button.setEnabled(is_checksum) # only allow "Next" button if checksum is good. Note this is different behavior than Electron Cash and Electrum which allows bad checksum biip39

    def on_skip_button(self):
        if self.parent.question(_('As the old adage says: \n\n"No backup, no bitcoin"\n\nAre you sure you wish to skip this step? (You will be offered the opportunity to backup your seed later.)')):
            self.was_skipped = True
            self.parent.next_button.setEnabled(True)
            self.parent.next_button.click()


class KeysLayout(QVBoxLayout):
    def __init__(self, parent=None, title=None, is_valid=None, allow_multi=False):
        QVBoxLayout.__init__(self)
        self.parent = parent
        self.is_valid = is_valid
        self.text_e = ScanQRTextEdit(allow_multi=allow_multi)
        self.text_e.textChanged.connect(self.on_edit)
        self.addWidget(WWLabel(title))
        self.addWidget(self.text_e)

    def get_text(self):
        return self.text_e.text()

    def on_edit(self):
        b = self.is_valid(self.get_text())
        self.parent.next_button.setEnabled(b)


class AbstractSeedDialog(WindowModalDialog):
    def __init__(self, parent, seed, passphrase, *, wallet=None):
        super().__init__(parent, ('Electron Cash - ' + _('Seed')))
        self.wallet = wallet
        self.seed = seed
        self.passphrase = passphrase
        self.setMinimumWidth(400)


class SeedDialog(AbstractSeedDialog):
    def __init__(self, parent, seed, passphrase, *, wallet=None):
        super().__init__(parent, seed, passphrase, wallet=wallet)
        vbox = QVBoxLayout(self)
        title =  _("Your wallet generation seed is:")
        slayout = SeedLayout(title=title, seed=seed, msg=True, passphrase=passphrase, editable=False)
        vbox.addLayout(slayout)
        vbox.addLayout(Buttons(CloseButton(self)))


class SeedBackupDialog(AbstractSeedDialog):
    def __init__(self, parent, seed, passphrase, *, wallet=None):
        super().__init__(parent, seed, passphrase, wallet=wallet)
        assert self.wallet is not None
        self.vbox = vbox = QVBoxLayout(self)
        title =  _("<b>Warning:</b> Your wallet generation seed has <i>not</i> yet been confirmed to have been backed-up by you!  It is important you save your seed somewhere (perferably on paper).<br><br>In order to confirm that your seed is backed-up, please write your seed down and proceed to the next screen:<br><br>")
        self.slayout_widget = QWidget()
        vbox2 = QVBoxLayout(self.slayout_widget)
        vbox2.setContentsMargins(0,0,0,0)
        slayout = SeedLayout(title=title, seed=seed, msg=True, passphrase=passphrase, editable=False, parent=self)
        vbox2.addLayout(slayout)
        vbox.addWidget(self.slayout_widget)
        self.next_button = next_button = QPushButton(_("Next"))
        next_button.clicked.connect(self.on_next)
        self.buttons=Buttons(CancelButton(self), next_button)
        vbox.addLayout(self.buttons)

    def on_next(self):
        # remove the old layout
        self.vbox.removeWidget(self.slayout_widget)
        self.slayout_widget.setParent(None)
        # mogrify next button to 'Confirm'
        self.next_button.clicked.disconnect(self.on_next)
        self.next_button.setText(_("Confirm"))
        self.next_button.setEnabled(False)
        self.next_button.clicked.connect(self.on_confirmed_backup)
        self.slayout_widget = QWidget()
        vbox2 = QVBoxLayout(self.slayout_widget)
        vbox2.setContentsMargins(0,0,0,0)
        title = _('To make sure that you have properly saved your seed, please retype it here.') + "<br><br>"
        slayout = SeedLayout(title=title, seed=None, msg=False, passphrase=self.passphrase, editable=True, parent=self)
        vbox2.addLayout(slayout)
        self.vbox.insertWidget(0, self.slayout_widget)


    def on_confirmed_backup(self):
        self.wallet.storage.put('wallet_seed_needs_backup', False)
        self.accept()
