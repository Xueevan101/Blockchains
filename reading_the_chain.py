from web3 import Web3
from web3.providers.rpc import HTTPProvider
import requests
import json

bayc_address = "0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D"
contract_address = Web3.to_checksum_address(bayc_address)

with open('ape_abi.json', 'r') as f:
    abi = json.load(f)

api_url = "https://mainnet.infura.io/v3/YOUR_INFURA_PROJECT_ID"
provider = HTTPProvider(api_url)
web3 = Web3(provider)

contract = web3.eth.contract(address=contract_address, abi=abi)

def get_ape_info(ape_id):
    assert isinstance(ape_id, int), f"{ape_id} is not an int"
    assert 0 <= ape_id, f"{ape_id} must be at least 0"
    assert 9999 >= ape_id, f"{ape_id} must be less than 10,000"

    data = {'owner': "", 'image': "", 'eyes': ""}

    try:
        owner = contract.functions.ownerOf(ape_id).call()
        token_uri = contract.functions.tokenURI(ape_id).call()
        if token_uri.startswith("ipfs://"):
            token_uri = token_uri.replace("ipfs://", "https://ipfs.io/ipfs/")
        response = requests.get(token_uri)
        response.raise_for_status()
        metadata = response.json()
        image = metadata.get('image', "")
        if image.startswith("ipfs://"):
            image = image.replace("ipfs://", "https://ipfs.io/ipfs/")
        eyes = ""
        for attr in metadata.get('attributes', []):
            if attr.get('trait_type') == "Eyes":
                eyes = attr.get('value')
                break
        data = {
            'owner': owner,
            'image': image,
            'eyes': eyes
        }
    except Exception as e:
        print(f"Error retrieving ape {ape_id}: {e}")

    assert isinstance(data, dict), f'get_ape_info({ape_id}) should return a dict'
    assert all([a in data.keys() for a in ['owner', 'image', 'eyes']]), f"return value should include the keys 'owner','image' and 'eyes'"
    return data
