from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
from datetime import datetime
import json
import pandas as pd


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = f"https://api.avax-test.network/ext/bc/C/rpc" #AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = f"https://data-seed-prebsc-1-s1.binance.org:8545/" #BSC testnet

    if chain in ['source','destination']:
        w3 = Web3(Web3.HTTPProvider(api_url))
        # inject the poa compatibility middleware to the innermost layer
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


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
        Scan the last 5 blocks of the source and destination chains
        Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
        When Deposit events are found on the source chain, call the 'wrap' function the destination chain
        When Unwrap events are found on the destination chain, call the 'withdraw' function on the source chain
    """
    # This is different from Bridge IV where chain was "avax" or "bsc"
    if chain not in ['source','destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # --- use your provided helper (no load_contract_info / get_contract) ---
    src_info = get_contract_info("source", contract_info)
    dst_info = get_contract_info("destination", contract_info)
    if not src_info or not dst_info:
        print("contract_info.json missing source/destination entries")
        return 0

    # Connect both chains
    w3_src = connect_to("source")
    w3_dst = connect_to("destination")

    # Build contract instances directly
    src_c = w3_src.eth.contract(
        address=Web3.to_checksum_address(src_info["address"]),
        abi=src_info["abi"]
    )
    dst_c = w3_dst.eth.contract(
        address=Web3.to_checksum_address(dst_info["address"]),
        abi=dst_info["abi"]
    )

    # Warden key from env (0x-prefixed)
    import os
    from eth_account import Account
    pk = os.environ.get("WARDEN_PRIVKEY")
    if not pk:
        print("WARDEN_PRIVKEY not set in environment")
        return 0
    acct_src = Account.from_key(pk)
    acct_dst = Account.from_key(pk)

    # Helper to send a tx
    def _send_tx(w3, acct, fn):
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        })
        # estimate gas with fallback
        try:
            g = w3.eth.estimate_gas(tx)
            tx["gas"] = int(g * 12 // 10)
        except Exception:
            tx["gas"] = 400000
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.rawTransaction)
        return h.hex()

    BLOCK_WINDOW = 5
    processed = 0

    if chain == "source":
        # Watch Fuji for Deposit → call wrap on BNB
        latest = w3_src.eth.block_number
        frm = max(0, latest - BLOCK_WINDOW)
        to = latest
        print(f"[source] Scanning Fuji blocks {frm}-{to} for Deposit...")
        try:
            deposits = src_c.events.Deposit().get_logs(fromBlock=frm, toBlock=to)
        except Exception as e:
            print(f"[source] get_logs Deposit failed: {e}")
            deposits = []

        if not deposits:
            print("[source] No Deposit events found.")
            return 0

        for ev in deposits:
            token = ev["args"]["token"]
            recipient = ev["args"]["recipient"]
            amount = int(ev["args"]["amount"])
            print(f"[source] Deposit → wrap on BNB: token={token} recipient={recipient} amount={amount}")
            try:
                txh = _send_tx(w3_dst, acct_dst, dst_c.functions.wrap(token, recipient, amount))
                print(f"[source] wrap() tx: {txh}")
                processed += 1
            except Exception as e:
                print(f"[source] wrap() failed: {e}")

    else:  # chain == "destination"
        # Watch BNB for Unwrap → call withdraw on Fuji
        latest = w3_dst.eth.block_number
        frm = max(0, latest - BLOCK_WINDOW)
        to = latest
        print(f"[destination] Scanning BNB blocks {frm}-{to} for Unwrap...")
        try:
            unwraps = dst_c.events.Unwrap().get_logs(fromBlock=frm, toBlock=to)
        except Exception as e:
            print(f"[destination] get_logs Unwrap failed: {e}")
            unwraps = []

        if not unwraps:
            print("[destination] No Unwrap events found.")
            return 0

        for ev in unwraps:
            args = ev["args"]
            underlying = args.get("underlying") or args.get("underlying_token") or args.get("token")
            recipient = args.get("recipient") or args.get("to")
            amount = args.get("amount")
            if underlying is None or recipient is None or amount is None:
                print(f"[destination] Unwrap args not understood: {args}")
                continue
            amount = int(amount)
            print(f"[destination] Unwrap → withdraw on Fuji: token={underlying} recipient={recipient} amount={amount}")
            try:
                txh = _send_tx(w3_src, acct_src, src_c.functions.withdraw(underlying, recipient, amount))
                print(f"[destination] withdraw() tx: {txh}")
                processed += 1
            except Exception as e:
                print(f"[destination] withdraw() failed: {e}")

    return processed
