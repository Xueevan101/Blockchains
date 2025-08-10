from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
from eth_account import Account
import json
import time

# -----------------------
# Warden key (TESTNET ONLY)
# -----------------------
WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"
WARDEN_ADDRESS = Account.from_key(WARDEN_PRIVKEY).address  # auto-derived

# Scan tuning (avoid rate limits but don’t miss events)
SCAN_BLOCKS = 60      # how many recent blocks to scan each run
CHUNK_SIZE  = 30      # blocks per filter call when chunking


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
    Uses legacy gasPrice for broad testnet compatibility (BSC + AVAX).
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
            return receipt
        except Exception as e:
            last_err = e
            time.sleep(2)

    raise RuntimeError(f"Failed to send transaction after retries: {last_err}")


def _scan_last_n_blocks(w3, contract, event_obj, n_blocks=SCAN_BLOCKS, chunk_size=CHUNK_SIZE):
    """
    Efficient, rate-limit-friendly scanner using a SINGLE RPC call per range or chunk:
      - Try one filter over the whole [start, head] range and call get_all_entries() once.
      - If the provider rejects that (common on BSC/Fuji), chunk the range and make ONE call per chunk.
    Returns a list of decoded events (AttributeDict with .args).
    """
    head = w3.eth.block_number
    if head < 0:
        return []

    start = max(0, head - n_blocks + 1)
    if start > head:
        return []

    # 1) Try one call for the full window
    try:
        flt = event_obj.create_filter(from_block=start, to_block=head)
        return flt.get_all_entries()  # single RPC returning all events
    except Exception:
        pass  # likely provider throttle or "limit exceeded"

    # 2) Fallback: chunked filters, still one call per chunk
    all_entries = []
    cur = start
    while cur <= head:
        to_blk = min(head, cur + chunk_size - 1)

        # short retry loop per chunk
        for attempt in range(3):
            try:
                flt = event_obj.create_filter(from_block=cur, to_block=to_blk)
                batch = flt.get_all_entries()  # single RPC per chunk
                if batch:
                    all_entries.extend(batch)
                break
            except Exception:
                time.sleep(1.0 + 0.5 * attempt)
                if attempt == 2:
                    # skip this slice if it keeps failing
                    pass

        cur = to_blk + 1

    return all_entries


# -----------------------
# Main bridge logic
# -----------------------

def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - (string) should be either "source" or "destination"
    Scan recent blocks:
      - If chain == 'source': look for 'Deposit' events on the source chain and call 'wrap' on the destination chain
      - If chain == 'destination': look for 'Unwrap' events on the destination chain and call 'withdraw' on the source chain
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

    if chain == 'source':
        # 1) Find Deposit(token, recipient, amount) on SOURCE
        try:
            deposit_event = src_contract.events.Deposit
        except AttributeError:
            print("ABI missing 'Deposit' event on source.")
            return 0

        deposits = _scan_last_n_blocks(w3_src, src_contract, deposit_event, n_blocks=SCAN_BLOCKS, chunk_size=CHUNK_SIZE)
        if not deposits:
            print("No Deposit events found on source in the recent window.")
            return 0

        # 2) For each Deposit, call wrap(token, recipient, amount) on DESTINATION
        for ev in deposits:
            token = ev.args.token
            recipient = ev.args.recipient
            amount = ev.args.amount

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
                print(f"wrap() tx sent on destination: {receipt.transactionHash.hex()}")
            except Exception as e:
                print(f"wrap() failed on destination: {e}")

    elif chain == 'destination':
        # 1) Find Unwrap(token, recipient, amount) on DESTINATION
        try:
            unwrap_event = dst_contract.events.Unwrap
        except AttributeError:
            print("ABI missing 'Unwrap' event on destination.")
            return 0

        unwraps = _scan_last_n_blocks(w3_dst, dst_contract, unwrap_event, n_blocks=SCAN_BLOCKS, chunk_size=CHUNK_SIZE)
        if not unwraps:
            print("No Unwrap events found on destination in the recent window.")
            return 0

        # 2) For each Unwrap, call withdraw(token, recipient, amount) on SOURCE
        for ev in unwraps:
            token = ev.args.token
            recipient = ev.args.recipient
            amount = ev.args.amount

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
                print(f"withdraw() tx sent on source: {receipt.transactionHash.hex()}")
            except Exception as e:
                print(f"withdraw() failed on source: {e}")

    return 1
