"""
Background jobber to proxy search and batch download for graph transactions.

This is used by slp_validator_0x01.py and slp_validator_0x01_nft1.py.
"""

import sys
import threading
import queue
import traceback
import weakref
import collections
import requests
import json
import base64
from .transaction import Transaction
from .caches import ExpiringCache

class SlpGraphSearch:
    """
    A single thread that processes graph search requests sequentially.
    """
    def __init__(self, network, wallet, threadname="SlpGraphSearch", errors='print'):
        self.network = network
        self.wallet = wallet
        self.errors = errors
        # ---
        self.graph_search_queue = queue.Queue()
        self.txn_dl_queue = queue.Queue()
        self.downloads = 0

        # Kick off the thread
        self.thread = threading.Thread(target=self.mainloop, name=threadname, daemon=True)
        self.thread.start()

    def mainloop(self,):
        try:
            while True:
                try:
                    job = self.graph_search_queue.get()
                except queue.Empty:
                    continue
                else:
                    txid, callback = job
                    try:
                        txid_requests = self.graph_search_query(txid)
                    except Exception as e:
                        # If query dies, keep going.
                        print("error in graph search query", e, file=sys.stderr)
                        pass
                    else:
                        if(txid_requests):
                            self.network.send(txid_requests, self.txn_dl_queue.put)

                        for _ in txid_requests: # fetch as many responses as were requested.
                            try:
                                resp = self.txn_dl_queue.get(True, timeout=60)
                            except queue.Empty: # timeout
                                break
                            if resp.get('error'):
                                if self.errors == "print":
                                    print("Tx request error:", resp.get('error'), file=sys.stderr)
                                elif self.errors == "raise":
                                    raise RuntimeError("Tx request error", resp.get('error'))
                                else:
                                    raise ValueError(self.errors)
                                continue
                            raw = resp.get('result')
                            tx = Transaction(raw)
                            Transaction.tx_cache_put(tx)
                            self.downloads += 1
                            print(str(self.downloads) + " transactions downloaded")
        finally:
            print("SearchGraph thread died!", file=sys.stderr)

    def add_search_job(self, txid, callback):
        """ Callback called as `callback(txid, results)`
        where txid is where to start graph search. """
        self.graph_search_queue.put((txid, callback))
        return txid

    def graph_search_query(self, txid):
        requrl = self.get_graphsearch_url(txid)
        print(requrl, file=sys.stderr)
        reqresult = requests.get(requrl, timeout=30)
        resp = json.loads(reqresult.content.decode('utf-8'))['g'][0]
        # response from slpserve will be a list of transactions
        #  necessary to complete validation along with depth
        ret = {}
        sorted_txids = [ txid for _,txid in sorted(zip(resp['depths'],resp['dependsOn'])) ]
        txid_requests = []
        for txid in sorted_txids:
            try:
                self.wallet.transactions[txid]
                continue
            except KeyError:
                pass
            if not Transaction.tx_cache_get(txid):
                txid_requests.append(('blockchain.transaction.get', [txid]))
        return txid_requests

    def get_graphsearch_url(self, txid):
        print(txid)
        host_url = "http://slpdb.fountainhead.cash/q/"
        q = {"v": 3, "q": { "db": ["g"],
            "aggregate": [
                {"$match": {
                        "graphTxn.txid": txid
                }},
                {"$graphLookup": {
                        "from": "graphs",
                        "startWith": "$graphTxn.txid",
                        "connectFromField": "graphTxn.txid",
                        "connectToField": "graphTxn.outputs.spendTxid",
                        "as": "dependsOn",
                        "maxDepth": 1000,
                        "depthField": "depth"
                        }
                },
                {"$project": {
                        "_id":0,
                        "tokenId": "$tokenDetails.tokenIdHex",
                        "txid": "$graphTxn.txid",
                        "dependsOn": {
                            "$map":{
                            "input": "$dependsOn.graphTxn.txid",
                            "in": "$$this"
                            }
                        },
                        "depths": {
                            "$map":{
                            "input": "$dependsOn.depth",
                            "in": "$$this"
                            }
                        }
                    }
            }],"limit": 1000 }}
        s = json.dumps(q)
        q = base64.b64encode(s.encode('utf-8'))
        url = host_url + q.decode('utf-8')
        print(url)
        return url
