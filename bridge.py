from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
from eth_account import Account
import json
import time

WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"
WARDEN_ADDRESS = Account.from_key(WARDEN_PRIVKEY).address  # auto-derived

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
            return receipt
        except Exception as e:
            last_err = e
            # brief backoff on nonce/gas issues
            time.sleep(2)

    raise RuntimeError(f"Failed to send transaction after retries: {last_err}")


def _event_topic(contract, event_name):
    """Compute the keccak topic for the event by name using the loaded ABI."""
    ev_abi = None
    for e in contract.abi:
        if e.get("type") == "event" and e.get("name") == event_name:
            ev_abi = e
            break
    if ev_abi is None:
        raise ValueError(f"Event {event_name} not found in ABI")
    return event_abi_to_log_topic(ev_abi)

def _get_logs_batched(w3, address, topic0, start, end, batch_size=50, max_retries=3):
    """Reliable batched eth_getLogs with backoff to avoid -32005 on BSC/Fuji."""
    logs = []
    current = start
    while current <= end:
        to_block = min(end, current + batch_size - 1)
        params = {
            "fromBlock": current,
            "toBlock": to_block,
            "address": address,
            "topics": [topic0]
        }
        retries = 0
        while True:
            try:
                batch = w3.eth.get_logs(params)
                logs.extend(batch)
                break
            except Exception as e:
                retries += 1
                if retries > max_retries:
                    # skip this slice and continue; provider may be flaky
                    break
                time.sleep(1.5 * retries)
        current = to_block + 1
    return logs

def _scan_last_n_blocks(w3, contract, event_obj, n_blocks=50):
    """
    Robust scan for the last n blocks for the given event using eth_getLogs.
    Falls back to per-block if needed.
    """
    head = w3.eth.block_number
    start = max(0, head - n_blocks + 1)
    address = contract.address
    # event name from the bound event
    event_name = event_obj.event_name
    topic0 = _event_topic(contract, event_name)

    # Primary path: small batched getLogs
    entries = _get_logs_batched(w3, address, topic0, start, head, batch_size=25, max_retries=3)
    if entries:
        # Decode logs with the contract’s event ABI
        decoded = []
        for lg in entries:
            try:
                decoded.append(event_obj().process_log(lg))
            except Exception:
                pass
        return decoded

    # Final fallback: per-block (rarely needed)
    found = []
    for block_num in range(start, head + 1):
        try:
            items = w3.eth.get_logs({
                "fromBlock": block_num,
                "toBlock": block_num,
                "address": address,
                "topics": [topic0]
            })
            for lg in items:
                try:
                    found.append(event_obj().process_log(lg))
                except Exception:
                    pass
        except Exception:
            pass
    return found

def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - (string) should be either "source" or "destination"
    Scan the last 5 blocks:
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

        deposits = _scan_last_n_blocks(w3_src, src_contract, deposit_event, n_blocks=5)
        if not deposits:
            print("No Deposit events found on source in the last 5 blocks.")
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

        unwraps = _scan_last_n_blocks(w3_dst, dst_contract, unwrap_event, n_blocks=5)
        if not unwraps:
            print("No Unwrap events found on destination in the last 5 blocks.")
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
