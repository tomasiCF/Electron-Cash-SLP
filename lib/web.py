# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 Thomas Voegtlin
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

from decimal import Decimal as PyDecimal  # Qt 5.12 also exports Decimal
import os
import re
import shutil
import threading
import urllib

from .address import Address
from . import bitcoin
from . import networks
from .util import format_satoshis_plain


DEFAULT_EXPLORER = "Bitcoin.com"

mainnet_block_explorers = {
    'Bitcoin.com': ('https://explorer.bitcoin.com/bch',
                    Address.FMT_CASHADDR,
                    {'tx': 'tx', 'addr': 'address'}),
    'Blockchair.com': ('https://blockchair.com/bitcoin-cash',
                       Address.FMT_CASHADDR,
                       {'tx': 'transaction', 'addr': 'address'}),
    'BTC.com': ('https://bch.btc.com',
                       Address.FMT_CASHADDR,
                       {'tx': '', 'addr': ''}),
    'ViaBTC.com': ('https://www.viabtc.com/bch',
                   Address.FMT_CASHADDR,
                   {'tx': 'tx', 'addr': 'address'})
}

DEFAULT_EXPLORER_TESTNET = 'Bitcoin.com'

testnet_block_explorers = {
    'Bitcoin.com'   : ('https://explorer.bitcoin.com/tbch',
                       Address.FMT_LEGACY,  # For some reason testnet expects legacy and fails on bchtest: addresses.
                       {'tx': 'tx', 'addr': 'address'}),
}

def BE_info():
    if networks.net.TESTNET:
        return testnet_block_explorers
    return mainnet_block_explorers

def BE_tuple(config):
    infodict = BE_info()
    return (infodict.get(BE_from_config(config))
            or infodict.get(BE_default_explorer()) # In case block explorer in config is bad/no longet valid
           )

def BE_default_explorer():
    return (DEFAULT_EXPLORER
            if not networks.net.TESTNET
            else DEFAULT_EXPLORER_TESTNET)

def BE_from_config(config):
    return config.get('block_explorer', BE_default_explorer())

def BE_URL(config, kind, item):
    be_tuple = BE_tuple(config)
    if not be_tuple:
        return
    url_base, addr_fmt, parts = be_tuple
    kind_str = parts.get(kind)
    if kind_str is None:
        return
    if kind == 'addr':
        assert isinstance(item, Address)
        item = item.to_string(addr_fmt)
    return "/".join(part for part in (url_base, kind_str, item) if part)

def BE_sorted_list():
    return sorted(BE_info())


def create_URI(addr, amount, message):
    if not isinstance(addr, Address):
        return ""
    scheme, path = addr.to_URI_components()
    query = []
    if amount:
        query.append('amount=%s'%format_satoshis_plain(amount))
    if message:
        query.append('message=%s'%urllib.parse.quote(message))
    p = urllib.parse.ParseResult(scheme=scheme,
                                 netloc='', path=path, params='',
                                 query='&'.join(query), fragment='')
    return urllib.parse.urlunparse(p)

# URL decode
#_ud = re.compile('%([0-9a-hA-H]{2})', re.MULTILINE)
#urldecode = lambda x: _ud.sub(lambda m: chr(int(m.group(1), 16)), x)

def parse_URI(uri, on_pr=None):
    if ':' not in uri:
        # Test it's valid
        Address.from_string(uri)
        return {'address': uri}

    if (uri.strip().lower().split(':', 1)[0] != networks.net.CASHADDR_PREFIX
        and uri.strip().lower().split(':', 1)[0] !=  networks.net.SLPADDR_PREFIX):
        raise Exception("Not a URI starting with '{}:' or '{}:'".format(networks.net.CASHADDR_PREFIX, networks.net.SLPADDR_PREFIX))

    u = urllib.parse.urlparse(uri)
    # The scheme always comes back in lower case
    address = u.path

    # python for android fails to parse query
    if address.find('?') > 0:
        address, query = u.path.split('?')
        pq = urllib.parse.parse_qs(query, keep_blank_values=True)
    else:
        pq = urllib.parse.parse_qs(u.query, keep_blank_values=True)

    for k, v in pq.items():
        if len(v)!=1:
            raise Exception('Duplicate Key', k)

    out = {k: v[0] for k, v in pq.items()}
    out['scheme'] = u.scheme
    if address:
        Address.from_string(address)
        out['address'] = address

    amounts = dict()
    for key in out:
        if 'amount' in key and key not in amounts:
            if '-' in out[key]:
                am = out[key].split('-', 1)[0]
                amount = PyDecimal(am)
                tokenparams = out[key].split('-', 1)[1]
            else:
                tokenparams = None
                amount = PyDecimal(out[key]) * bitcoin.COIN
            if tokenparams:
                tokenid = tokenparams.split('-', 1)[0]
                #TODO check regex of tokenid
                try:
                    tokenflags = tokenparams.split('-', 1)[1]
                    amounts[tokenid] = { 'amount': amount.real, 'tokenflags': tokenflags }
                except:
                    amounts[tokenid] = { 'amount': amount.real, 'tokenflags': None }
            else:
                amounts['bch'] = { 'amount': int(amount), 'tokenflags': None }
    if 'amount' in out:
        out.pop('amount')
    if len(amounts) > 0:
        out['amounts'] = amounts
    if len(amounts) > 1:
        raise Exception('This wallet does not yet support token payments with additional BCH send amounts.')
    if len(amounts) > 2:
        raise Exception('This wallet does not support more than 2 payment request amounts.')
    if 'message' in out:
        out['message'] = out['message']
        out['memo'] = out['message']
    if 'time' in out:
        out['time'] = int(out['time'])
    if 'exp' in out:
        out['exp'] = int(out['exp'])
    if 'sig' in out:
        out['sig'] = bh2u(bitcoin.base_decode(out['sig'], None, base=58))

    r = out.get('r')
    sig = out.get('sig')
    name = out.get('name')
    if on_pr and (r or (name and sig)):
        def get_payment_request_thread():
            from . import paymentrequest as pr
            if name and sig:
                s = pr.serialize_request(out).SerializeToString()
                request = pr.PaymentRequest(s)
            else:
                request = pr.get_payment_request(r)
            if on_pr:
                on_pr(request)
        t = threading.Thread(target=get_payment_request_thread)
        t.setDaemon(True)
        t.start()

    return out

def check_www_dir(rdir):
    if not os.path.exists(rdir):
        os.mkdir(rdir)
    index = os.path.join(rdir, 'index.html')
    if not os.path.exists(index):
        print_error("copying index.html")
        src = os.path.join(os.path.dirname(__file__), 'www', 'index.html')
        shutil.copy(src, index)
    files = [
        "https://code.jquery.com/jquery-1.9.1.min.js",
        "https://raw.githubusercontent.com/davidshimjs/qrcodejs/master/qrcode.js",
        "https://code.jquery.com/ui/1.10.3/jquery-ui.js",
        "https://code.jquery.com/ui/1.10.3/themes/smoothness/jquery-ui.css"
    ]
    for URL in files:
        path = urllib.parse.urlsplit(URL).path
        filename = os.path.basename(path)
        path = os.path.join(rdir, filename)
        if not os.path.exists(path):
            print_error("downloading ", URL)
            urllib.request.urlretrieve(URL, path)
