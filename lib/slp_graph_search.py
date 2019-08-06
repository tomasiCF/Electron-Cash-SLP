"""
Background search and batch download for graph transactions.

This is used by slp_validator_0x01.py.
"""

import sys
import threading
import queue
import traceback
import weakref
import collections
import json
import base64
import requests
from .transaction import Transaction
from .caches import ExpiringCache

class GraphSearchJob:
    def __init__(self, txid, valjob_ref):
        self.root_txid = txid
        self.valjob=valjob_ref

        # metadata fetched from back end
        self.depth_map = None
        self.total_depth= None
        self.txn_count_total = None

        # job status info
        self.search_started=False
        self.search_success=None
        self.job_complete=False
        self.error_msg=None
        self.depth_completed = 0
        self.depth_current_query = None
        self.txn_count_progress = 0
        self.last_search_url = None

    def get_metadata(self):
        res = self.metadata_query(self.root_txid, self.valjob.network.slpdb_host)
        self.total_depth = res['totalDepth']
        self.txn_count_total = res['txcount']
        self.depth_map = res['depthMap']

    def metadata_query(self, txid, slpdb_host):
        requrl = self.metadata_url([txid], slpdb_host)
        print("[SLP Graph Search] depth search url = " + requrl, file=sys.stderr)
        reqresult = requests.get(requrl, timeout=10)
        res = dict()
        for resp in json.loads(reqresult.content.decode('utf-8'))['g']:
            o = { 'depthMap': resp['depthMap'], 'txcount': resp['txcount'], 'totalDepth': resp['totalDepth'] }
            res = o
        return res

    def metadata_url(self, txids, host):
        txids_q = []
        for txid in txids:
            txids_q.append({"graphTxn.txid": txid})
        q = {
            "v": 3,
            "q": {
                "aggregate": [
                    {"$match": {"$or": txids_q}},
                    {"$project": {
                            "_id": 0,
                            "txid": "$graphTxn.txid",
                            "txcount": "$graphTxn.stats.txcount",
                            "totalDepth": "$graphTxn.stats.depth",
                            "depthMap": "$graphTxn.stats.depthMap"
                        }
                    }
                ],
                "limit": len(txids)
            }
        }
        s = json.dumps(q)
        q = base64.b64encode(s.encode('utf-8'))
        url = host + "/q/" + q.decode('utf-8')
        return url


class SlpGraphSearchManager:
    """
    A single thread that processes graph search requests sequentially.
    """
    def __init__(self, threadname="SlpGraphSearch"):
        # holds the job history and status
        self.search_jobs = dict()

        # Create a single use queue on a new thread
        self.new_job_queue = queue.Queue()  # this is a queue for performing metadata 
        self.search_queue = queue.Queue()
        self.thread = None
        self.threadname=threadname

    def new_search(self, valjob_ref):
        """ start a search job on new thread, returns weakref of new GS jobber object"""
        txid = valjob_ref.root_txid
        if txid not in self.search_jobs.keys():
            job = GraphSearchJob(txid, valjob_ref)
            self.search_jobs[txid] = job
            self.new_job_queue.put(job)
            if not self.thread:
                self.thread = threading.Thread(target=self.mainloop, name=self.threadname, daemon=True)
                self.thread.start()
            return job
        return None

    def mainloop(self,):
        try:
            while True:
                # NOTE: the purpose of inner while loop is to fetch graph metadata and prioritize search jobs based on metadata results (see TODO below.)
                while True:
                    try:
                        _job = self.new_job_queue.get(block=False)
                        if not _job.valjob.network.slpdb_host:
                            raise Exception("SLPDB host not set")
                        _job.get_metadata()
                    except queue.Empty:
                        break
                    except Exception as e:
                        print("error in graph search query", str(e), file=sys.stderr)
                        continue
                    else:
                        self.search_queue.put(_job)
                    
                        # TODO IF new job queue is finally empty, here we should prioritize order of search jobs queue based on:
                        #       (1) remove any items whose validation job has finished
                        #       (2) sort queue by DAG size, largest jobs will benefit from GS the most.
                        #       (3) check to see if the root_txid is already in validity cache from previous job

                try:
                    job = self.search_queue.get(block=False)
                except queue.Empty:
                    if self.new_job_queue.empty():
                        self.thread = None
                        break
                else:
                    try:
                        # TODO: before starting job, check to see if the root_txid is already in validity cache from previous job

                        # search_query is a recursive call, most time will be spent here
                        job.search_started = True
                        self.search_query(job)
                    except Exception as e:
                        print("error in graph search query", e, file=sys.stderr)
                        job.error_msg = str(e)
                        job.search_success = False
                        job.job_complete = True
                        return
                    else:
                        pass
        finally:
            print("[SLP Graph Search] SearchGraph thread completed.", file=sys.stderr)

    def search_query(self, job, txids=None, depth_map_index=0):
        if depth_map_index == 0:
            txids = [job.root_txid]
        job.depth_current_query, txn_count = job.depth_map[str((depth_map_index+1)*1000)]  # we query for chunks with up to 1000 txns
        if depth_map_index > 0:
            queryDepth = job.depth_current_query - job.depth_map[str((depth_map_index)*1000)][0]
            txn_count = txn_count - job.depth_map[str((depth_map_index)*1000)][1]
        else:
            queryDepth = job.depth_current_query
        # f = open("gs-"+job.root_txid+".txt","a")
        # f.write(str(queryDepth)+","+str(job.depth_current_query)+","+str(txn_count)+"\n")
        # f.write("==== Graph Search Query ===="+"\n")
        # f.write("txids: "+str(txids)+"\n")
        # f.write("total depth: "+str(job.total_depth)+"\n")
        # f.write("this query's depth: "+str(job.depth_current_query)+"\n")
        # f.write("expected query txn count: "+str(txn_count)+"\n")
        # f.write("query depth: "+str(queryDepth)+"\n")
        # f.write("txn search url = " + requrl+"\n")
        # f.write("============================"+"\n")
        requrl = self.search_url(txids, queryDepth, job.valjob.network.slpdb_host) #TODO: handle 'validity_cache' exclusion from graph search (NOTE: this will impact total dl count)
        job.last_search_url = requrl
        reqresult = requests.get(requrl, timeout=60)
        job.depth_completed = job.depth_map[str((depth_map_index+1)*1000)][0]
        dependsOn = []
        depths = []
        for resp in json.loads(reqresult.content.decode('utf-8'))['g']:
            dependsOn.extend(resp['dependsOn'])
            depths.extend(resp['depths'])
        txns = [ (d, Transaction(base64.b64decode(tx).hex())) for d,tx in zip(depths, dependsOn) ]
        job.txn_count_progress+=len(txns)
        for tx in txns:
            SlpGraphSearchManager.tx_cache_put(tx[1])
        if job.depth_completed < job.total_depth:
            # TODO: check to see if the validation job is still running, if not then should raise ValidationJobFinished and conitinue in while loop
            txids = [ tx[1].txid_fast() for tx in txns if tx[0] == queryDepth ]
            depth_map_index+=1
            self.search_query(job, txids, depth_map_index)
        else:
            job.search_success = True
            job.job_complete = True
            print("[SLP Graph Search] job success")

    def search_url(self, txids, max_depth, host, validity_cache=[]):
        print("[SLP Graph Search] " + str(txids))
        txids_q = []
        for txid in txids:
            txids_q.append({"graphTxn.txid": txid})
        q = {
            "v": 3,
            "q": {
                "db": ["g"],
                "aggregate": [
                    {"$match": {"$or": txids_q}},
                    {"$graphLookup": {
                        "from": "graphs",
                        "startWith": "$graphTxn.txid",
                        "connectFromField": "graphTxn.txid",
                        "connectToField": "graphTxn.outputs.spendTxid",
                        "as": "dependsOn",
                        "maxDepth": max_depth,
                        "depthField": "depth",
                        "restrictSearchWithMatch": { #TODO: add tokenId restriction to this for NFT1 application
                            "graphTxn.txid": {"$nin": validity_cache}} 
                    }},
                    {"$project":{
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
                    },
                    {"$unwind": {
                        "path": "$dependsOn", "includeArrayIndex": "depends_index"
                        }
                    },
                    {"$unwind":{
                        "path": "$depths", "includeArrayIndex": "depth_index"
                        }
                    },
                    {"$project": {
                        "tokenId": 1,
                        "txid": 1,
                        "dependsOn": 1,
                        "depths": 1,
                        "compare": {"$cmp":["$depends_index", "$depth_index"]}
                        }
                    },
                    {"$match": {
                        "compare": 0
                        }
                    },
                    {"$group": {
                        "_id":"$dependsOn",
                        "txid": {"$first": "$txid"},
                        "tokenId": {"$first": "$tokenId"},
                        "depths": {"$push": "$depths"}
                        }
                    },
                    {"$lookup": {
                        "from": "confirmed",
                        "localField": "_id",
                        "foreignField": "tx.h",
                        "as": "tx"
                        }
                    },
                    {"$project": {
                        "txid": 1,
                        "tokenId": 1,
                        "depths": 1,
                        "dependsOn": "$tx.tx.raw",
                        "_id": 0
                        }
                    },
                    {
                        "$unwind": "$dependsOn"
                    },
                    {
                        "$unwind": "$depths"
                    },
                    {
                        "$sort": {"depths": 1}
                    },
                    {
                        "$group": {
                            "_id": "$txid",
                            "dependsOn": {"$push": "$dependsOn"},
                            "depths": {"$push": "$depths"},
                            "tokenId": {"$first": "$tokenId"}
                        }
                    },
                    {
                        "$project": {
                            "txid": "$_id",
                            "tokenId": 1,
                            "dependsOn": 1,
                            "depths": 1,
                            "_id": 0, 
                            "txcount": { "$size": "$dependsOn" }
                        }
                    }
                ],
                "limit": 2000 #len(txids)
            }
            }
        s = json.dumps(q)
        q = base64.b64encode(s.encode('utf-8'))
        url = host + "/q/" + q.decode('utf-8')
        return url

    # This cache stores foreign (non-wallet) tx's we fetched from the network
    # for the purposes of the "fetch_input_data" mechanism. Its max size has
    # been thoughtfully calibrated to provide a decent tradeoff between
    # memory consumption and UX.
    #
    # In even aggressive/pathological cases this cache won't ever exceed
    # 100MB even when full. [see ExpiringCache.size_bytes() to test it].
    # This is acceptable considering this is Python + Qt and it eats memory
    # anyway.. and also this is 2019 ;). Note that all tx's in this cache
    # are in the non-deserialized state (hex encoded bytes only) as a memory
    # savings optimization.  Please maintain that invariant if you modify this
    # code, otherwise the cache may grow to 10x memory consumption if you
    # put deserialized tx's in here.
    _fetched_tx_cache = ExpiringCache(maxlen=100000, name="GraphSearchTxnFetchCache")

    @classmethod
    def tx_cache_get(cls, txid : str) -> object:
        ''' Attempts to retrieve txid from the tx cache that this class
        keeps in-memory.  Returns None on failure. The returned tx is
        not deserialized, and is a copy of the one in the cache. '''
        tx = cls._fetched_tx_cache.get(txid)
        if tx is not None and tx.raw:
            # make sure to return a copy of the transaction from the cache
            # so that if caller does .deserialize(), *his* instance will
            # use up 10x memory consumption, and not the cached instance which
            # should just be an undeserialized raw tx.
            return Transaction(tx.raw)
        return None

    @classmethod
    def tx_cache_put(cls, tx : object, txid : str = None):
        ''' Puts a non-deserialized copy of tx into the tx_cache. '''
        if not tx or not tx.raw:
            raise ValueError('Please pass a tx which has a valid .raw attribute!')
        txid = txid or Transaction._txid(tx.raw)  # optionally, caller can pass-in txid to save CPU time for hashing
        cls._fetched_tx_cache.put(txid, Transaction(tx.raw))