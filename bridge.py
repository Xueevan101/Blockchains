from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
from eth_account import Account
import json
import time
import os

# -----------------------
# Warden key (TESTNET ONLY)
# -----------------------
WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"
WARDEN_ADDRESS = Account.from_key(WARDEN_PRIVKEY).address  # auto-derived

# Small files to avoid reprocessing and missing windows
STATE_FILE = "bridge_state.json"
PROCESSED_FILE = "processed_events.json"


# -----------------------
# Connection + helpers
# -----------------------

def connect_to(chain):
    """
    chain: 'source' (Avalanche Fuji) or 'destination' (BSC Testnet)
    """
    if chain == 'source':  # AVAX C-chain testnet
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"
    elif chain == 'destination':  # BSC testnet
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    else:
        raise ValueError("Invalid chain. Use 'source' or 'destination'.")

    w3 = Web3(Web3.HTTPProvider(api_url))
    # POA compatibility for both testnets
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
    Load the contract_info file into a dictionary and return section for given chain.
    Expected keys under each chain: { "address": "...", "abi": [ ... ] }
    """
    try:
        with open(contract_info, 'r') as f:
            contracts = json.load(f)
    except Exception as e:
        print(f"Failed to read contract info\nPlease contact your instructor\n{e}")
        return 0
    return contracts[chain]


def _contract_for(w3, chain_info):
    """
    Create a contract object for the deployed address + ABI.
    """
    addr = Web3.to_checksum_address(chain_info["address"])
    abi = chain_info["abi"]
    return w3.eth.contract(address=addr, abi=abi)


def _get_raw_tx(signed):
    """
    Web3.py v5 uses 'rawTransaction'; v6 uses 'raw_transaction'.
    """
    raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raise AttributeError("SignedTransaction missing raw tx (neither rawTransaction nor raw_transaction present).")
    return raw


def _build_and_send_tx(w3, contract_fn, sender_addr, sender_key, value=0, gas_buffer=20000, max_retries=2):
    """
    Builds, signs, and sends a transaction for the given contract function.
    Uses legacy gasPrice for broad testnet compatibility (works on BSC + AVAX).
    Retries on common nonce/gas hiccups.
    """
    try:
        gas_estimate = contract_fn.estimate_gas({"from": sender_addr, "value": value})
    except Exception:
        gas_estimate = 300000  # fallback

    gas = gas_estimate + gas_buffer
    last_err = None

    for attempt in range(max_retries + 1):
        try:
            nonce = w3.eth.get_transaction_count(sender_addr)
            tx = contract_fn.build_transaction({
                "from": sender_addr,
                "nonce": nonce,
                "gas": gas,
                "gasPrice": w3.eth.gas_price,
                "value": value,
                "chainId": w3.eth.chain_id
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=sender_key)
            raw = _get_raw_tx(signed)
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            # Only treat as success if it actually succeeded
            if getattr(receipt, "status", 0) != 1:
                raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
            return receipt
        except Exception as e:
            last_err = e
            time.sleep(2)

    raise RuntimeError(f"Failed to send transaction after retries: {last_err}")


def _extract_bridge_args(ev):
    """
    Normalize event args across ABIs: handles token/underlying/underlying_token,
    recipient/to, amount/value.
    """
    args = ev.args

    # token
    token = getattr(args, "token", None)
    if token is None:
        token = getattr(args, "underlying", None)
    if token is None:
        token = getattr(args, "underlying_token", None)

    # recipient
    recipient = getattr(args, "recipient", None)
    if recipient is None:
        recipient = getattr(args, "to", None)

    # amount
    amount = getattr(args, "amount", None)
    if amount is None:
        amount = getattr(args, "value", None)

    if token is None or recipient is None or amount is None:
        # Try mapping-style access if available
        try:
            keys = list(args.keys())
        except Exception:
            keys = []
        raise ValueError(f"Unrecognized event arg names; available: {keys}")

    token = Web3.to_checksum_address(token)
    recipient = Web3.to_checksum_address(recipient)
    amount = int(amount)
    return token, recipient, amount


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"source_last": 0, "destination_last": 0}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _load_processed():
    try:
        return set(json.load(open(PROCESSED_FILE)))
    except Exception:
        return set()


def _save_processed(s):
    json.dump(sorted(list(s)), open(PROCESSED_FILE, "w"))


def _scan_last_n_blocks(w3, contract, event_obj, n_blocks=5, argument_filters=None):
    """
    Return decoded event logs for the last n blocks using filters + get_all_entries()
    to minimize RPC calls. Falls back to small chunking if the provider enforces limits.
    """
    if argument_filters is None:
        argument_filters = {}

    head = w3.eth.block_number
    start = max(0, head - n_blocks + 1)

    def _filter_once(fb, tb):
        flt = event_obj.create_filter(from_block=fb, to_block=tb, argument_filters=argument_filters)
        return flt.get_all_entries()

    try:
        return _filter_once(start, head)
    except Exception:
        pass

    CHUNK = 250
    found = []
    cur = start
    while cur <= head:
        to_blk = min(cur + CHUNK - 1, head)
        try:
            entries = _filter_once(cur, to_blk)
            found.extend(entries)
        except Exception:
            try:
                blkflt = event_obj.create_filter(from_block=cur, to_block=cur, argument_filters=argument_filters)
                found.extend(blkflt.get_all_entries())
            except Exception:
                pass
        cur = to_blk + 1

    return found


def _scan_from_last(w3, event_obj, state_key, safety_back=200, argument_filters=None):
    """
    Persistent range scanning: start from last scanned+1 (or head - safety_back on first run),
    chunk via filters->get_all_entries, and update the state.
    """
    if argument_filters is None:
        argument_filters = {}

    state = _load_state()
    head = w3.eth.block_number
    start = (state.get(state_key) or max(0, head - safety_back)) + 1
    if start > head:
        return [], state, head, start

    def _filter_once(fb, tb):
        flt = event_obj.create_filter(from_block=fb, to_block=tb, argument_filters=argument_filters)
        return flt.get_all_entries()

    CHUNK = 500
    found = []
    cur = start
    while cur <= head:
        to_blk = min(cur + CHUNK - 1, head)
        try:
            entries = _filter_once(cur, to_blk)
            found.extend(entries)
        except Exception:
            # fallback to smaller chunks
            mid = (cur + to_blk) // 2
            for fb, tb in [(cur, mid), (mid + 1, to_blk)]:
                try:
                    entries = _filter_once(fb, tb)
                    found.extend(entries)
                except Exception:
                    # last resort: per-block
                    for b in range(fb, tb + 1):
                        try:
                            flt = event_obj.create_filter(from_block=b, to_block=b, argument_filters=argument_filters)
                            found.extend(flt.get_all_entries())
                        except Exception:
                            pass
        cur = to_blk + 1

    # Update state to the head we actually scanned to
    state[state_key] = head
    _save_state(state)
    return found, state, head, start


# -----------------------
# Main bridge logic
# -----------------------

def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - (string) should be either "source" or "destination"
    When 'source': scan for Deposit and call wrap on destination
    When 'destination': scan for Unwrap and call withdraw on source
    Uses persistent scanning to avoid window misses and dedupes events to avoid double-processing.
    """
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # Load per-chain sections
    try:
        with open(contract_info, 'r') as f:
            contracts_all = json.load(f)
    except Exception as e:
        print(f"Failed to read contract info\nPlease contact your instructor\n{e}")
        return 0

    src_info = contracts_all["source"]
    dst_info = contracts_all["destination"]

    # Connect RPCs + contract objects
    w3_src = connect_to("source")
    w3_dst = connect_to("destination")
    src_contract = _contract_for(w3_src, src_info)
    dst_contract = _contract_for(w3_dst, dst_info)

    # Use the hardcoded warden key/address on BOTH chains
    warden_addr_src = Web3.to_checksum_address(WARDEN_ADDRESS)
    warden_key_src = WARDEN_PRIVKEY
    warden_addr_dst = warden_addr_src
    warden_key_dst = warden_key_src

    processed = _load_processed()

    if chain == 'source':
        # 1) Find Deposit(...) on SOURCE
        try:
            deposit_event = src_contract.events.Deposit
        except AttributeError:
            print("ABI missing 'Deposit' event on source.")
            return 0

        # Persistent scan to avoid missing events due to short windows
        deposits, state, head, start = _scan_from_last(
            w3_src, deposit_event, state_key="source_last", safety_back=200
        )
        if not deposits:
            print(f"No Deposit events found on source in blocks {start}-{head}.")
            return 0

        # 2) For each Deposit, call wrap(token, recipient, amount) on DESTINATION
        for ev in deposits:
            evt_id = f"{ev.transactionHash.hex()}:{ev.logIndex}"
            if evt_id in processed:
                continue

            try:
                token, recipient, amount = _extract_bridge_args(ev)
            except Exception as e:
                print(f"Could not parse Deposit event: {e}")
                continue

            print(f"[{w3_src.eth.block_number}] Source Deposit -> token={token}, recipient={recipient}, amount={amount}")

            try:
                fn = dst_contract.functions.wrap(token, recipient, amount)
            except Exception as e:
                print(f"Destination contract missing 'wrap' or wrong ABI: {e}")
                continue

            try:
                receipt = _build_and_send_tx(
                    w3_dst,
                    fn,
                    sender_addr=warden_addr_dst,
                    sender_key=warden_key_dst
                )
                print(f"wrap() confirmed on destination (block {receipt.blockNumber}): {receipt.transactionHash.hex()}")
                processed.add(evt_id)
            except Exception as e:
                print(f"wrap() failed on destination: {e}")

        _save_processed(processed)

    elif chain == 'destination':
        # 1) Find Unwrap(...) on DESTINATION
        try:
            unwrap_event = dst_contract.events.Unwrap
        except AttributeError:
            print("ABI missing 'Unwrap' event on destination.")
            return 0

        unwraps, state, head, start = _scan_from_last(
            w3_dst, unwrap_event, state_key="destination_last", safety_back=200
        )
        if not unwraps:
            print(f"No Unwrap events found on destination in blocks {start}-{head}.")
            return 0

        # 2) For each Unwrap, call withdraw(token, recipient, amount) on SOURCE
        for ev in unwraps:
            evt_id = f"{ev.transactionHash.hex()}:{ev.logIndex}"
            if evt_id in processed:
                continue

            try:
                token, recipient, amount = _extract_bridge_args(ev)
            except Exception as e:
                print(f"Could not parse Unwrap event: {e}")
                continue

            print(f"[{w3_dst.eth.block_number}] Destination Unwrap -> token={token}, recipient={recipient}, amount={amount}")

            try:
                fn = src_contract.functions.withdraw(token, recipient, amount)
            except Exception as e:
                print(f"Source contract missing 'withdraw' or wrong ABI: {e}")
                continue

            try:
                receipt = _build_and_send_tx(
                    w3_src,
                    fn,
                    sender_addr=warden_addr_src,
                    sender_key=warden_key_src
                )
                print(f"withdraw() confirmed on source (block {receipt.blockNumber}): {receipt.transactionHash.hex()}")
                processed.add(evt_id)
            except Exception as e:
                print(f"withdraw() failed on source: {e}")

        _save_processed(processed)

    return 1
