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
        print( f"Invalid chain: {chain}" )
        return 0
    
    ci = load_contract_info(contract_info)
    src_info = ci["source"]
    dst_info = ci["destination"]

    # Connect both chains (we need both regardless of direction to send the counterpart tx)
    w3_src = connect_to("source")
    w3_dst = connect_to("destination")

    # Contracts
    src_c = get_contract(w3_src, src_info["address"], src_info["abi"])
    dst_c = get_contract(w3_dst, dst_info["address"], dst_info["abi"])

    # Signer (warden)
    acct = get_signer(os.environ.get("PRIV_ENV", DEFAULT_PRIV_ENV))

    if chain == "source":
        # The grader created activity on SOURCE; we bridge to DESTINATION
        handle_source_deposits(w3_src, w3_dst, acct, src_c, dst_c)
    else:
        # The grader created activity on DESTINATION; we bridge to SOURCE
        handle_destination_unwraps(w3_dst, w3_src, acct, dst_c, src_c)
