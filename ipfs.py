import requests
import json

def pin_to_ipfs(data):
	assert isinstance(data, dict), "Error pin_to_ipfs expects a dictionary"

	url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"

	headers = {
        "pinata_api_key": "YOUR_API_KEY",
        "pinata_secret_api_key": "YOUR_API_SECRET",
        "Content-Type": "application/json"
	}

	response = requests.post(url, headers=headers, json=json_data)

	if response.ok:
		ipfs_hash = response.json()['IpfsHash']
	return f"https://gateway.pinata.cloud/ipfs/{ipfs_hash}"
	else:
		raise Exception(f"Failed to pin data: {response.text}")



def get_from_ipfs(cid,content_type="json"):
	assert isinstance(cid,str), f"get_from_ipfs accepts a cid in the form of a string"
	#YOUR CODE HERE	
	url = f"https://gateway.pinata.cloud/ipfs/{cid}"

	response = requests.get(url)

	if response.ok:
		if content_type == "json":
			data = response.json()
	else:
		raise ValueError("Only 'json' content_type is supported at the moment")
	else:
		raise Exception(f"Failed to retrieve content from IPFS: {response.text}")
	assert isinstance(data,dict), f"get_from_ipfs should return a dict"
	return data
