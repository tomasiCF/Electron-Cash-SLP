import requests
import threading
import json
import sys
import queue
from .slp import SlpMessage, buildSendOpReturnOutput_V1
from .slp_coinchooser import SlpCoinChooser

class SlpPostOffice:

    @staticmethod
    def build_slp_msg_for_rates(wallet, config, tokenId, po_data, send_amount):

        # determine the amount of postage to pay based on the token's rate and number of inputs we will sign
        weight = po_data["weight"]
        rate = None
        for stamp in po_data["stamps"]:
            if stamp["tokenId"] == tokenId:
                rate = stamp["rate"]

        if rate is None:
            raise Exception("Post Office does not offer postage for tokenId: " + tokenId)

        # variables used for txn size estimation
        slpmsg_output_max_size = 8 + 1 + 73                 # case where both postage and change are needed
        slpmsg_output_mid_size = slpmsg_output_max_size - 9 # case where no token change is not needed
        slpmsg_output_min_size = slpmsg_output_mid_size - 9 # case where no token or change are needed
        output_unit_size = 34                               # p2pkh output size
        input_unit_size_ecdsa = 149                         # approx. size needed for ecdsa signed input
        input_unit_size_schnorr = 141                       # approx. size needed for schnorr signed input
        txn_overhead = 4 + 1 + 1 + 4                        # txn version, input count varint, output count varint, timelock

        # determine number of stamps required in this while loop
        sats_diff_w_fee = 1    # controls entry into while loop
        stamp_count = -1        # will get updated to 0 stamps in first iteration
        while sats_diff_w_fee > 0:
            stamp_count += 1
            coins, _ = SlpCoinChooser.select_coins(wallet, tokenId, (send_amount + (rate * stamp_count)), config)
            
            output_dust_count = 1
            slpmsg_output_size = slpmsg_output_min_size
            postage_amt = rate * stamp_count
            total_coin_value = 0
            for coin in coins:
                total_coin_value += coin["token_value"]
            change_amt = total_coin_value - send_amount

            if postage_amt > 0 and change_amt > 0:
                output_dust_count = 3
                slpmsg_output_size = slpmsg_output_max_size
            elif postage_amt > 0 or change_amt > 0:
                output_dust_count = 2
                slpmsg_output_size = slpmsg_output_mid_size
            
            txn_size_wo_stamps = txn_overhead + input_unit_size_ecdsa * len(coins) + output_unit_size * output_dust_count + slpmsg_output_size

            # output cost differential (positive value means we need stamps)
            output_sats_diff = (output_dust_count * 546) - (len(coins) * 546)

            # fee cost differential (positive value means we need more stamps)
            fee_rate = 1
            sats_diff_w_fee = (txn_size_wo_stamps * fee_rate) + output_sats_diff - stamp_count * weight

        if output_dust_count == 1:
            amts = [send_amount]
        elif output_dust_count == 2 and postage_amt > 0:
            amts = [send_amount, postage_amt]
        elif output_dust_count == 2 and change_amt > 0:
            amts = [send_amount, change_amt]
        elif output_dust_count == 3:
            amts = [send_amount, postage_amt, change_amt]
        else:
            raise Exception("Unhandled exception")

        slp_output = buildSendOpReturnOutput_V1(tokenId, amts)

        return coins, slp_output

    @staticmethod
    def sign_inputs_for_po_server(tx, wallet):
        """
        Signs and returns incomplete transaction for a post office to complete
        """
        # TODO
        return

    @staticmethod
    def sign_inputs_from_payment_request(pr, wallet):
        """
        Signs and returns incomplete transaction for a payment request
        """
        # TODO
        return

class SlpPostOfficeClient:
    """
    An SLP post office client to interact with a single post office server.
    """
    def __init__(self, hosts=[]):
        self.post_office_hosts = hosts
        self.ban_list = []
        self.postage_data = {}
        self.optimized_rates = {}

        self.task_queue = queue.Queue()
        self.fetch_thread = threading.Thread(target=self.mainloop, name='SlpPostOfficeClient', daemon=True)
        self.fetch_thread.start()

        self.update_all_postage_urls()

    def update_all_postage_urls(self):
        for url in self.post_office_hosts:
            self.task_queue.put(url)

    def _set_postage(self, host, _json):
        try:
            j = json.loads(_json)
        except json.decoder.JSONDecodeError:
            if host in self.postage_data.keys():
                self.postage_data.pop(host)
        else:
            self.postage_data[host] = j

    def _fetch_postage_json(self, host):
        res = requests.get(host + "/postage", timeout=5)
        self._set_postage(host, res.text)

    def mainloop(self):
        try:
            while True:
                url = self.task_queue.get(block=True)
                self._fetch_postage_json(url)
                self.optimize_rates()
        finally:
            print("[SLP Post Office Client] Error: mainloop exited.", file=sys.stderr)

    def optimize_rates(self):
        token_rates = {}
        for host in self.postage_data.keys():
            try:
                stamps = self.postage_data[host]["stamps"]
            except KeyError:
                continue
            else:
                for stamp in stamps:
                    tokenId = stamp["tokenId"]
                    if tokenId not in token_rates.keys():
                        token_rates[tokenId] = []
                    token_rates[tokenId].append(stamp)
        
        for token in token_rates:
            sorted(token_rates[token], key=lambda i: i['rate'])
        
        self.optimized_rates = token_rates

    def ban_post_office(self, url):
        if not url in self.ban_list:
            self.ban_list.append(url)

    def allow_post_office(self, url):
        if url in self.ban_list:
            self.ban_list.remove(url)
