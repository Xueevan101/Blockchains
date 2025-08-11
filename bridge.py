from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
import json
import time
import os

# -----------------------
# Config
# -----------------------

# TESTNET-ONLY warden key; never hardcode in prod
WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"
WARDEN_ADDRESS = Account.from_key(WARDEN_PRIVKEY).address

# State on disk
STATE_FILE = "bridge_state.json"
PROCESSED_FILE = "processed_events.json"

# Scanner behavior
BASE_CHUNK = 500          # starting chunk size for range scans
SAFETY_BACK = 200         # on first run, rewind this many blocks
MAX_RPC_ERRORS = 25       # hard cap to break out of loops
MAX_SECONDS = 25          # total time budget per scan call; set 0 to disable
DEBUG_SCAN_RANGES = False # set True to print ranges tried


# -----------------------
# Web3 helpers
# -----------------------

def connect_to(chain: str) -> Web3:
    """Connect to Fuji or BSC testnet with POA compatibility."""
    if chain == 'source':
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"
    elif chain == 'destination':
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    else:
        raise ValueError("Invalid chain. Use 'source' or 'destination'.")

    w3 = Web3(Web3.HTTPProvider(api_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info_file):
    """Load {address, abi} for the given chain from JSON file."""
    with open(contract_info_file, 'r') as f:
        contracts = json.load(f)
    return contracts[chain]


def _contract_for(w3: Web3, chain_info):
    """Instantiate contract object from address + ABI."""
    addr = Web3.to_checksum_address(chain_info["address"])
    return w3.eth.contract(address=addr, abi=chain_info["abi"])


def _get_raw_tx(signed):
    """Support web3.py v5 and v6 signed tx objects."""
    raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raise AttributeError("SignedTransaction missing raw tx")
    return raw


def _build_and_send_tx(w3, contract_fn, sender_addr, sender_key, value=0, gas_buffer=20000, max_retries=2):
    """Send a tx with retries and verify success."""
    try:
        gas_estimate = contract_fn.estimate_gas({"from": sender_addr, "value": value})
    except Exception:
        gas_estimate = 300000

    gas = gas_estimate + gas_buffer
    last_err = None

    for _ in range(max_retries + 1):
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
            tx_hash = w3.eth.send_raw_transaction(_get_raw_tx(signed))
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if getattr(receipt, "status", 0) != 1:
                raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
            return receipt
        except Exception as e:
            last_err = e
            time.sleep(2)

    raise RuntimeError(f"Failed to send transaction after retries: {last_err}")


# -----------------------
# Event utils
# -----------------------

def _extract_bridge_args(ev):
    """Normalize event arg names across ABI variants."""
    args = ev.args
    token = getattr(args, "token", getattr(args, "underlying", getattr(args, "underlying_token", None)))
    recipient = getattr(args, "recipient", getattr(args, "to", None))
    amount = getattr(args, "amount", getattr(args, "value", None))

    if token is None or recipient is None or amount is None:
        try:
            keys = list(args.keys())
        except Exception:
            keys = []
        raise ValueError(f"Unrecognized event arg names; available: {keys}")

    return (
        Web3.to_checksum_address(token),
        Web3.to_checksum_address(recipient),
        int(amount),
    )


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


# -----------------------
# Rate-limit-safe scanner with aborts
# -----------------------

def _scan_from_last(
    w3,
    event_obj,
    state_key,
    safety_back=SAFETY_BACK,
    argument_filters=None,
    base_chunk=BASE_CHUNK,
    max_rpc_errors=MAX_RPC_ERRORS,
    max_seconds=MAX_SECONDS,
    debug=DEBUG_SCAN_RANGES,
):
    """
    Persistent, RPC-friendly scan:
      - Starts at last_scanned+1 (or head-safety_back on first run)
      - Uses create_filter(...).get_all_entries() in chunks
      - On RPC errors: backoff + shrink chunk; skip block if needed
      - Hard caps: max_rpc_errors, max_seconds (prevents infinite loops)
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

    found = []
    cur = start
    chunk = max(1, int(base_chunk))
    rpc_errors = 0
    t0 = time.time()

    while cur <= head:
        if max_seconds and (time.time() - t0) > max_seconds:
            print("[ABORT] Scan timed out; returning partial results.")
            break

        hi = min(cur + chunk - 1, head)
        if debug:
            print(f"[scan] trying {cur}-{hi} (chunk={chunk})")

        try:
            entries = _filter_once(cur, hi)
            found.extend(entries)

            # Success: cautiously grow chunk back toward base
            if chunk < base_chunk:
                chunk = min(base_chunk, chunk * 2)

            cur = hi + 1

        except Exception as e:
            rpc_errors += 1
            msg = str(e)
            print(f"[RPC-ERR] range {cur}-{hi}: {msg} (errors {rpc_errors}/{max_rpc_errors})")

            # Backoff if looks like throttling
            lower = msg.lower()
            if any(x in lower for x in ["limit", "throttle", "429", "rate"]):
                # exponential backoff capped at 30s
                sleep_s = min(2 ** min(rpc_errors, 5), 30)
                time.sleep(sleep_s)

            if rpc_errors >= max_rpc_errors:
                print("[ABORT] Too many RPC errors; returning partial results.")
                break

            # Shrink chunk; if already 1 (per-block) and still failing, SKIP the block
            if chunk > 1:
                chunk = max(1, chunk // 2)
            else:
                # skip this problematic block and continue
                cur = hi + 1

    # Persist progress (the highest block we actually advanced beyond)
    state[state_key] = max(state.get(state_key, start - 1), min(head, cur - 1))
    _save_state(state)
    return found, state, head, start


# -----------------------
# Main bridge logic
# -----------------------

def scan_blocks(chain, contract_info="contract_info.json"):
    """
    - 'source': read Deposit events, call wrap(token, recipient, amount) on destination
    - 'destination': read Unwrap events, call withdraw(token, recipient, amount) on source
    Scanner is resistant to RPC limits and won’t loop forever.
    """
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    try:
        with open(contract_info, 'r') as f:
            contracts_all = json.load(f)
    except Exception as e:
        print(f"Failed to read contract info\nPlease contact your instructor\n{e}")
        return 0

    src_info = contracts_all["source"]
    dst_info = contracts_all["destination"]

    # Web3 + contracts
    w3_src = connect_to("source")
    w3_dst = connect_to("destination")
    src_contract = _contract_for(w3_src, src_info)
    dst_contract = _contract_for(w3_dst, dst_info)

    # Warden on both chains (testnet simplicity)
    warden_addr_src = Web3.to_checksum_address(WARDEN_ADDRESS)
    warden_addr_dst = warden_addr_src

    processed = _load_processed()

    if chain == 'source':
        # (1) Scan Deposits on SOURCE
        try:
            deposit_event = src_contract.events.Deposit
        except AttributeError:
            print("ABI missing 'Deposit' event on source.")
            return 0

        deposits, state, head, start = _scan_from_last(
            w3_src, deposit_event, state_key="source_last"
        )
        if not deposits:
            print(f"No Deposit events found on source in blocks {start}-{head}.")
            return 0

        # (2) For each Deposit, call wrap on DESTINATION
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
                rcpt = _build_and_send_tx(
                    w3_dst,
                    dst_contract.functions.wrap(token, recipient, amount),
                    sender_addr=warden_addr_dst,
                    sender_key=WARDEN_PRIVKEY
                )
                print(f"wrap() confirmed on destination (block {rcpt.blockNumber}): {rcpt.transactionHash.hex()}")
                processed.add(evt_id)
            except Exception as e:
                print(f"wrap() failed on destination: {e}")

        _save_processed(processed)

    else:  # chain == 'destination'
        # (1) Scan Unwraps on DESTINATION
        try:
            unwrap_event = dst_contract.events.Unwrap
        except AttributeError:
            print("ABI missing 'Unwrap' event on destination.")
            return 0

        unwraps, state, head, start = _scan_from_last(
            w3_dst, unwrap_event, state_key="destination_last"
        )
        if not unwraps:
            print(f"No Unwrap events found on destination in blocks {start}-{head}.")
            return 0

        # (2) For each Unwrap, call withdraw on SOURCE
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
                rcpt = _build_and_send_tx(
                    w3_src,
                    src_contract.functions.withdraw(token, recipient, amount),
                    sender_addr=warden_addr_src,
                    sender_key=WARDEN_PRIVKEY
                )
                print(f"withdraw() confirmed on source (block {rcpt.blockNumber}): {rcpt.transactionHash.hex()}")
                processed.add(evt_id)
            except Exception as e:
                print(f"withdraw() failed on source: {e}")

        _save_processed(processed)

    return 1
