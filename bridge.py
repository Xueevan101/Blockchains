from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
import json
import os

# ── Warden key (TESTNET ONLY) ────────────────────────────────────────────────
# Put your private key here (0x...) OR leave blank and export:
#   export WARDEN_PRIVKEY=0xYOUR_TESTNET_PRIVATE_KEY
WARDEN_PRIVKEY = "0xef1f86da85c3cd7822a0ce378a7abbd024c516f45ed9ad48b4cc9556cbb4e2f2"  # <-- paste 0x... here if you don't want to use env var

# Keep tiny to avoid RPC "limit exceeded" on public endpoints
BLOCK_WINDOW = 2


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"  # AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"  # BSC testnet

    if chain in ['source','destination']:
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
        with open(contract_info, 'r')  as f:
            contracts = json.load(f)
    except Exception as e:
        print( f"Failed to read contract info\nPlease contact your instructor\n{e}" )
        return 0
    return contracts[chain]


def scan_blocks(chain, contract_info="contract_info.json"):
    """
        chain - (string) should be either "source" or "destination"
        Scan the last few blocks of the source and destination chains
        Look for 'Deposit' events on the source chain and 'Unwrap' (or 'Withdrawal') on the destination chain
        When Deposit events are found on the source chain, call 'wrap' on the destination chain
        When Unwrap/Withdrawal events are found on the destination chain, call 'withdraw' on the source chain
    """

    # This is different from Bridge IV where chain was "avax" or "bsc"
    if chain not in ['source','destination']:
        print( f"Invalid chain: {chain}" )
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

    # Helper: compute topic0 for an event (by ABI)
    def _topic_for(event):
        ev_abi = event._get_event_abi()
        ev_name = ev_abi["name"]
        ev_inputs = ",".join(inp["type"] for inp in ev_abi["inputs"])
        return Web3.keccak(text=f"{ev_name}({ev_inputs})").hex()

    # helper: OR of multiple topic0s (EVM accepts list-of-list for OR on the same index)
    def _topics_or(*topics_hex):
        arr = []
        for t in topics_hex:
            if t and t.startswith("0x"):
                arr.append(t)
        return [arr] if arr else None

    # Version-agnostic event fetching via eth_getLogs + process_log, with per-block fallback
    def fetch_events(w3, contract, topics0, address, frm, to, decoders):
        """
        topics0: list of topic0 hex strings to OR together at topics[0]
        decoders: list of `contract.events.X` accessors to try when decoding
        """
        addr = Web3.to_checksum_address(address)

        def _try_get_logs(_from, _to):
            return w3.eth.get_logs({
                "fromBlock": int(_from),
                "toBlock": int(_to),
                "address": addr,
                "topics": [_topics_or(*topics0)]
            })

        # Primary attempt
        try:
            raw_logs = _try_get_logs(frm, to)
        except Exception as e:
            emsg = str(e)
            if "limit exceeded" in emsg:
                raw_logs = []
                # per-block fallback to dodge RPC limits
                for b in range(int(frm), int(to) + 1):
                    try:
                        raw_logs += _try_get_logs(b, b)
                    except Exception as e2:
                        print(f"[logs] per-block get_logs failed at {b}: {e2}")
            else:
                print(f"[logs] get_logs failed: {e}")
                return []

        decoded = []
        for log in raw_logs:
            dec = None
            for d in decoders:
                try:
                    dec = d().process_log(log)
                    break
                except Exception:
                    dec = None
            if dec:
                decoded.append(dec)
        return decoded

    processed = 0
    BLOCKS = BLOCK_WINDOW

    if chain == "source":
        # Watch Fuji for Deposit -> call wrap on BNB
        latest = w3_src.eth.block_number
        frm = max(0, latest - BLOCKS)
        to  = latest
        print(f"[source] Scanning Fuji blocks {frm}-{to} for Deposit events...")

        topic_deposit = _topic_for(src_c.events.Deposit)
        deposits = fetch_events(
            w3_src,
            src_c,
            topics0=[topic_deposit],
            address=src_info["address"],
            frm=frm,
            to=to,
            decoders=[src_c.events.Deposit]
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
        # Watch BNB for Unwrap (or Withdrawal) -> call withdraw on Fuji
        latest = w3_dst.eth.block_number
        frm = max(0, latest - BLOCKS)
        to  = latest
        print(f"[destination] Scanning BNB blocks {frm}-{to} for Unwrap events...")

        topic_unwrap = None
        topic_withdrawal = None
        try:
            topic_unwrap = _topic_for(dst_c.events.Unwrap)
        except Exception:
            pass
        try:
            topic_withdrawal = _topic_for(dst_c.events.Withdrawal)
        except Exception:
            pass

        topics = [t for t in [topic_unwrap, topic_withdrawal] if t]
        if not topics:
            print("[destination] Neither Unwrap nor Withdrawal events found in ABI.")
            return 0

        decoders = []
        if topic_unwrap:
            decoders.append(dst_c.events.Unwrap)
        if topic_withdrawal:
            decoders.append(dst_c.events.Withdrawal)

        unwraps = fetch_events(
            w3_dst,
            dst_c,
            topics0=topics,
            address=dst_info["address"],
            frm=frm,
            to=to,
            decoders=decoders
        )

        if not unwraps:
            print("[destination] No Unwrap events found.")
            return 0

        for ev in unwraps:
            args = ev["args"]
            # robust field handling
            underlying = args.get("underlying") or args.get("underlying_token") or args.get("token")
            recipient  = args.get("recipient") or args.get("to")
            amount     = args.get("amount")
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
