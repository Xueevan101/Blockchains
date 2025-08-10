from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
import json, time, os

WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"
WARDEN_ADDRESS = Account.from_key(WARDEN_PRIVKEY).address

def connect_to(chain):
    if chain == 'source':
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"
    elif chain == 'destination':
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    else:
        raise ValueError("Invalid chain.")
    w3 = Web3(Web3.HTTPProvider(api_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3

def _contract_for(w3, info):
    return w3.eth.contract(address=Web3.to_checksum_address(info["address"]), abi=info["abi"])

def _get_raw_tx(signed):
    return getattr(signed, "rawTransaction", getattr(signed, "raw_transaction", None)) or \
           (_ for _ in ()).throw(AttributeError("SignedTransaction missing raw tx"))

def _build_and_send_tx(w3, contract_fn, sender_addr, sender_key, value=0, gas_buffer=20000, max_retries=2):
    try:
        gas_est = contract_fn.estimate_gas({"from": sender_addr, "value": value})
    except Exception:
        gas_est = 300000
    gas = gas_est + gas_buffer
    last_err = None
    for _ in range(max_retries + 1):
        try:
            tx = contract_fn.build_transaction({
                "from": sender_addr,
                "nonce": w3.eth.get_transaction_count(sender_addr),
                "gas": gas,
                "gasPrice": w3.eth.gas_price,
                "value": value,
                "chainId": w3.eth.chain_id
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=sender_key)
            tx_hash = w3.eth.send_raw_transaction(_get_raw_tx(signed))
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if getattr(rcpt, "status", 0) != 1:
                raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
            return rcpt
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"Failed to send transaction: {last_err}")

def _extract_bridge_args(ev):
    a = ev.args
    token = getattr(a, "token", getattr(a, "underlying", getattr(a, "underlying_token", None)))
    recipient = getattr(a, "recipient", getattr(a, "to", None))
    amount = getattr(a, "amount", getattr(a, "value", None))
    if token is None or recipient is None or amount is None:
        try: keys = list(a.keys())
        except: keys = []
        raise ValueError(f"Unrecognized event args; available: {keys}")
    return Web3.to_checksum_address(token), Web3.to_checksum_address(recipient), int(amount)

def _scan_from_last(w3, event_obj, last_scanned, safety_back=200, argument_filters=None):
    """Return (events, new_last_scanned). Uses chunked filters + get_all_entries()."""
    if argument_filters is None: argument_filters = {}
    head = w3.eth.block_number
    start = (last_scanned or max(0, head - safety_back)) + 1
    if start > head: return [], last_scanned
    CHUNK = 500
    found = []
    cur = start
    def fetch(fb, tb):
        flt = event_obj.create_filter(from_block=fb, to_block=tb, argument_filters=argument_filters)
        return flt.get_all_entries()
    while cur <= head:
        hi = min(cur + CHUNK - 1, head)
        try:
            found.extend(fetch(cur, hi))
        except Exception:
            mid = (cur + hi) // 2
            for fb, tb in [(cur, mid), (mid + 1, hi)]:
                try:
                    found.extend(fetch(fb, tb))
                except Exception:
                    for b in range(fb, tb + 1):
                        try:
                            found.extend(fetch(b, b))
                        except Exception:
                            pass
        cur = hi + 1
    return found, head

def scan_blocks(chain, contract_info="contract_info.json", state_file="bridge_state.json"):
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}"); return 0
    try:
        contracts_all = json.load(open(contract_info))
    except Exception as e:
        print(f"Failed to read contract info\nPlease contact your instructor\n{e}"); return 0

    src_info, dst_info = contracts_all["source"], contracts_all["destination"]
    w3_src, w3_dst = connect_to("source"), connect_to("destination")
    src_contract, dst_contract = _contract_for(w3_src, src_info), _contract_for(w3_dst, dst_info)

    warden_addr = Web3.to_checksum_address(WARDEN_ADDRESS)
    processed = set()  # in-memory dedupe for a single run

    # tiny state (last scanned heads)
    try: state = json.load(open(state_file))
    except: state = {"source_last": 0, "destination_last": 0}

    if chain == 'source':
        try: deposit_event = src_contract.events.Deposit
        except AttributeError: print("ABI missing 'Deposit' on source."); return 0

        deposits, new_last = _scan_from_last(w3_src, deposit_event, state.get("source_last", 0))
        if not deposits:
            print(f"No Deposit events found on source in blocks {(state.get('source_last') or 0)+1}-{w3_src.eth.block_number}."); return 0
        for ev in deposits:
            evt_id = f"{ev.transactionHash.hex()}:{ev.logIndex}"
            if evt_id in processed: continue
            try:
                token, recipient, amount = _extract_bridge_args(ev)
            except Exception as e:
                print(f"Could not parse Deposit event: {e}"); continue
            print(f"[{w3_src.eth.block_number}] Source Deposit -> token={token}, recipient={recipient}, amount={amount}")
            try:
                rcpt = _build_and_send_tx(w3_dst, dst_contract.functions.wrap(token, recipient, amount),
                                          sender_addr=warden_addr, sender_key=WARDEN_PRIVKEY)
                print(f"wrap() confirmed on destination (block {rcpt.blockNumber}): {rcpt.transactionHash.hex()}")
                processed.add(evt_id)
            except Exception as e:
                print(f"wrap() failed on destination: {e}")
        state["source_last"] = new_last

    else:  # destination
        try: unwrap_event = dst_contract.events.Unwrap
        except AttributeError: print("ABI missing 'Unwrap' on destination."); return 0

        unwraps, new_last = _scan_from_last(w3_dst, unwrap_event, state.get("destination_last", 0))
        if not unwraps:
            print(f"No Unwrap events found on destination in blocks {(state.get('destination_last') or 0)+1}-{w3_dst.eth.block_number}."); return 0
        for ev in unwraps:
            evt_id = f"{ev.transactionHash.hex()}:{ev.logIndex}"
            if evt_id in processed: continue
            try:
                token, recipient, amount = _extract_bridge_args(ev)
            except Exception as e:
                print(f"Could not parse Unwrap event: {e}"); continue
            print(f"[{w3_dst.eth.block_number}] Destination Unwrap -> token={token}, recipient={recipient}, amount={amount}")
            try:
                rcpt = _build_and_send_tx(w3_src, src_contract.functions.withdraw(token, recipient, amount),
                                          sender_addr=warden_addr, sender_key=WARDEN_PRIVKEY)
                print(f"withdraw() confirmed on source (block {rcpt.blockNumber}): {rcpt.transactionHash.hex()}")
                processed.add(evt_id)
            except Exception as e:
                print(f"withdraw() failed on source: {e}")
        state["destination_last"] = new_last

    json.dump(state, open(state_file, "w"))
    return 1
