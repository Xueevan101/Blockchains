from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
from datetime import datetime
import json
import os

# ── Warden key (TESTNET ONLY) ────────────────────────────────────────────────
# Put your private key here (0x...) OR leave blank and export:
#   export WARDEN_PRIVKEY=0xYOUR_TESTNET_PRIVATE_KEY
WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"  # <-- paste 0x... here if you don't want to use env var

# Small to avoid RPC limits / grader timeouts
BLOCK_WINDOW = 2


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"  # AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"  # BSC testnet

    if chain in ['source', 'destination']:
        w3 = Web3(Web3.HTTPProvider(api_url))
        # inject the poa compatibility middleware to the innermost layer
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return w3

    raise ValueError(f"Invalid chain: {chain}")


def get_contract_info(chain, contract_info):
    """
        Load the contract_info file into a dictionary
        This function is used by the autograder and will likely be useful to you
    """
    try:
        with open(contract_info, 'r') as f:
            contracts = json.load(f)
    except Exception as e:
        print(f"Failed to read contract info\nPlease contact your instructor\n{e}")
        return 0
    return contracts[chain]


def scan_blocks(chain, contract_info="contract_info.json"):
    """
        chain - (string) should be either "source" or "destination"
        Scan the last few blocks of the source and destination chains
        Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
        When Deposit events are found on the source chain, call 'wrap' on the destination chain
        When Unwrap events are found on the destination chain, call 'withdraw' on the source chain
    """

    # This is different from Bridge IV where chain was "avax" or "bsc"
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # Load contract metadata for both sides
    src_info = get_contract_info("source", contract_info)
    dst_info = get_contract_info("destination", contract_info)
    if not src_info or not dst_info:
        print("contract_info.json missing source/destination entries")
        return 0

    # RPC connections
    w3_src = connect_to("source")
    w3_dst = connect_to("destination")

    # Contracts
    try:
        src_c = w3_src.eth.contract(
            address=Web3.to_checksum_address(src_info["address"]),
            abi=src_info["abi"]
        )
        dst_c = w3_dst.eth.contract(
            address=Web3.to_checksum_address(dst_info["address"]),
            abi=dst_info["abi"]
        )
    except Exception as e:
        print(f"Failed to construct contract instances: {e}")
        return 0

    # Resolve warden private key (env first, then placeholder)
    pk = os.environ.get("WARDEN_PRIVKEY") or WARDEN_PRIVKEY
    if not pk:
        print("WARDEN_PRIVKEY not set in environment and WARDEN_PRIVKEY placeholder is empty")
        return 0
    if not pk.startswith("0x"):
        pk = "0x" + pk  # normalize to 0x-prefixed

    from eth_account import Account
    acct_src = Account.from_key(pk)
    acct_dst = Account.from_key(pk)

    # Nonce managers (pending, then increment locally)
    dst_nonce = w3_dst.eth.get_transaction_count(acct_dst.address, block_identifier="pending")
    src_nonce = w3_src.eth.get_transaction_count(acct_src.address, block_identifier="pending")

    # Helper: build, sign, send with gas estimate (+fallback) and nonce handling
    def _send_tx(w3, acct, fn, nonce):
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        })
        # estimate gas with a safe fallback
        try:
            g = w3.eth.estimate_gas(tx)
            tx["gas"] = int(g * 12 // 10)  # +20%
        except Exception:
            tx["gas"] = 400000

        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        if raw is None:
            raise RuntimeError("Could not find raw tx bytes on SignedTransaction")

        try:
            return w3.eth.send_raw_transaction(raw).hex()
        except Exception as e:
            # If replacement/nonce issues arise, bump gas price and retry once with SAME nonce
            msg = str(e)
            if ("replacement transaction underpriced" in msg) or ("nonce too low" in msg):
                try:
                    tx["gasPrice"] = int(int(tx["gasPrice"]) * 11 // 10) or (w3.eth.gas_price + 1_000_000_000)
                    signed2 = acct.sign_transaction(tx)
                    raw2 = getattr(signed2, "rawTransaction", None) or getattr(signed2, "raw_transaction", None)
                    return w3.eth.send_raw_transaction(raw2).hex()
                except Exception as e2:
                    raise e2
            raise

    # Version-agnostic event fetching via eth_getLogs + process_log
    def fetch_events(w3, contract, event, address, frm, to):
        """
        Use eth_getLogs with topic0 = keccak(event signature), decode via event().process_log.
        Works across Web3.py versions and avoids create_filter signature differences.
        Includes per-block fallback when RPC says "limit exceeded".
        """
        ev_abi = event._get_event_abi()
        ev_name = ev_abi["name"]
        ev_inputs = ",".join(inp["type"] for inp in ev_abi["inputs"])
        sig_text = f"{ev_name}({ev_inputs})".strip()  # e.g., "Deposit(address,address,uint256)"

        topic0 = Web3.keccak(text=sig_text).hex()
        if not topic0.startswith("0x"):
            topic0 = "0x" + topic0

        addr = Web3.to_checksum_address(address)

        def _try_get_logs(_from, _to):
            return w3.eth.get_logs({
                "fromBlock": int(_from),
                "toBlock": int(_to),
                "address": addr,
                "topics": [topic0]
            })

        # Primary attempt
        try:
            raw_logs = _try_get_logs(frm, to)
        except Exception as e:
            # detect "limit exceeded" (often ValueError with dict in args)
            emsg = str(e)
            if "limit exceeded" in emsg:
                raw_logs = []
                # per-block fallback
                for b in range(int(frm), int(to) + 1):
                    try:
                        raw_logs += _try_get_logs(b, b)
                    except Exception as e2:
                        print(f"[logs] per-block get_logs failed at {b} for {ev_name}: {e2}")
            else:
                print(f"[logs] get_logs failed for {ev_name}: {e}")
                return []

        decoded = []
        for log in raw_logs:
            try:
                decoded.append(event().process_log(log))
            except Exception as e:
                print(f"[logs] process_log failed for {ev_name}: {e}")
        return decoded

    processed = 0
    BLOCKS = BLOCK_WINDOW

    if chain == "source":
        # Watch Fuji for Deposit -> call wrap on BNB
        latest = w3_src.eth.block_number
        frm = max(0, latest - BLOCKS)
        to = latest
        print(f"[source] Scanning Fuji blocks {frm}-{to} for Deposit events...")

        deposits = fetch_events(
            w3_src, src_c, src_c.events.Deposit, src_info["address"], frm, to
        )

        if not deposits:
            print("[source] No Deposit events found.")
            return 0

        for ev in deposits:
            token = ev["args"]["token"]
            recipient = ev["args"]["recipient"]
            amount = int(ev["args"]["amount"])
            print(f"[source] Deposit → wrap on BNB: token={token} recipient={recipient} amount={amount}")
            try:
                txh = _send_tx(w3_dst, acct_dst, dst_c.functions.wrap(token, recipient, amount), nonce=dst_nonce)
                print(f"[source] wrap() tx: {txh}")
                dst_nonce += 1
                processed += 1
            except Exception as e:
                print(f"[source] wrap() failed: {e}")

    else:  # chain == "destination"
        # Watch BNB for Unwrap -> call withdraw on Fuji
        latest = w3_dst.eth.block_number
        frm = max(0, latest - BLOCKS)
        to = latest
        print(f"[destination] Scanning BNB blocks {frm}-{to} for Unwrap events...")

        unwraps = fetch_events(
            w3_dst, dst_c, dst_c.events.Unwrap, dst_info["address"], frm, to
        )

        if not unwraps:
            print("[destination] No Unwrap events found.")
            return 0

        for ev in unwraps:
            args = ev["args"]
            # robust field handling just in case names differ
            underlying = args.get("underlying") or args.get("underlying_token") or args.get("token")
            recipient = args.get("recipient") or args.get("to")
            amount = args.get("amount")
            if underlying is None or recipient is None or amount is None:
                print(f"[destination] Unwrap args not understood: {args}")
                continue

            amount = int(amount)
            print(f"[destination] Unwrap → withdraw on Fuji: token={underlying} recipient={recipient} amount={amount}")
            try:
                txh = _send_tx(w3_src, acct_src, src_c.functions.withdraw(underlying, recipient, amount), nonce=src_nonce)
                print(f"[destination] withdraw() tx: {txh}")
                src_nonce += 1
                processed += 1
            except Exception as e:
                print(f"[destination] withdraw() failed: {e}")

    return processed
