from electroncash.util import NotEnoughFundsSlp, NotEnoughUnfrozenFundsSlp, print_error
from electroncash import slp

class SlpWallet:
    @staticmethod
    def check_tx_slp(wallet, tx, *, coins_to_burn=None):
        """
        Testing:
            - [X] Test Non-SLP transaction with SLP inputs throws using Burn Tool to burn ALL of a coin, since that will produce a non-SLP output with SLP inputs
                    - requires removing "slp_coins_to_burn" param from "slp_burn_token_dialog.py" broadcast_transaction()
            - [ ] Test SLP transaction with too high of inputs throws using Burn Tool with some of a coin, since that will have more inputs than outputs.
            - [ ] Test SLP transaction with wrong SLP inputs throws by ________
            - [ ] Test SLP transaction with insufficient inputs throws by ________
            - [ ] Check BURN dialog
            - [ ] Check MINT dialog
            - [ ] Check SEND
            - [ ] Check GENESIS
        """

        try:
            slp_msg = slp.SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])
        except:
            slp_msg = None
            
        # check a non-SLP txn for SLP inputs (only allow slp inputs if specified in 'coins_to_burn')
        if not slp_msg:
            for txo in tx.inputs():
                addr = txo['address']
                prev_out = txo['prevout_hash']
                prev_n = txo['prevout_n']
                slp_txo = None
                with wallet.lock:
                    try:
                        slp_txo = wallet._slp_txo[addr][prev_out][prev_n]
                    except: 
                        pass
                if slp_txo:
                    is_burn_allowed = False
                    if coins_to_burn:
                        for c in coins_to_burn:
                            if c['prevout_hash'] == prev_out and c['prevout_n'] == prev_n:
                                is_burn_allowed = True
                                c['is_in_txn'] = True

                    if not is_burn_allowed:
                        print_error("SLP check failed for non-SLP transaction which contains SLP inputs.")
                        raise NonSlpTransactionHasSlpInputs

            # check that all coins within 'coins_to_burn' are included in burn transaction
            if coins_to_burn:
                for c in coins_to_burn:
                    try:
                        if c['is_in_txn']:
                            continue
                    except:
                        raise MissingCoinToBeBurned

        elif slp_msg:
            if slp_msg.transaction_type == 'SEND':
                tid = slp_msg.op_return_fields['token_id_hex']
                # raise an Exception if:
                #   - [ ] input quantity is greater than output quanitity (TODO: except for qty specified in 'coins_to_burn')
                #   - [X] input quantity is less than output quanitity
                #   - [X] slp input does not match tokenId
                slp_outputs = slp_msg.op_return_fields['token_output']
                input_slp_qty = 0
                for txo in tx.inputs():
                    addr = txo['address']
                    prev_out = txo['prevout_hash']
                    prev_n = txo['prevout_n']
                    with wallet.lock:
                        try: 
                            slp_input = wallet._slp_txo[addr][prev_out][prev_n]
                            input_slp_qty += slp_input['qty']
                            if slp_input['token_id'] != tid:
                                print_error("SLP check failed for SEND due to incorrect tokenId in txn input")
                                raise SlpWrongTokenInput
                        except:
                            pass
                if input_slp_qty < sum(slp_outputs):
                    print_error("SLP check failed for SEND due to insufficient SLP inputs")
                    raise SlpInputsTooLow
                elif input_slp_qty > sum(slp_outputs):
                    #TODO: except for qty specified in 'coins_to_burn'
                    print_error("SLP check failed for SEND due to SLP inputs too high")
                    raise SlpInputsTooHigh

            # return False if any other SLP inputs have been included
            elif slp_msg.transaction_type == 'MINT':
                print_error("SLP check failed for MINT")
                raise Exception("This final check is not yet implemented for MINT")

            elif slp_msg.transaction_type == 'GENESIS':
                print_error("SLP check failed for GENESIS")
                raise Exception("This final check is not yet implemented for GENESIS")

        # return True if this check passes
        print_error("Final SLP check passed")
        return True

# Exceptions caused by malformed or unexpected data found in parsing.
class SlpTransactionValidityError(Exception):
    pass

class NonSlpTransactionHasSlpInputs(SlpTransactionValidityError):
    # Cannot have SLP inputs in non-SLP transaction
    pass

class SlpWrongTokenInput(SlpTransactionValidityError):
    # Cannot have SLP inputs in non-SLP transaction
    pass

class SlpInputsTooLow(SlpTransactionValidityError):
    # SLP input quantity too low in SEND transaction
    pass

class SlpInputsTooHigh(SlpTransactionValidityError):
    # SLP input quantity too high in SEND transaction
    pass

class MissingCoinToBeBurned(SlpTransactionValidityError):
    # SLP input quantity too high in SEND transaction
    pass