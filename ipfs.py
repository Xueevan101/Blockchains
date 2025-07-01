import requests
import json

def pin_to_ipfs(data):
	assert isinstance(data, dict), "Error pin_to_ipfs expects a dictionary"

	url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"

	json_data = {
        "pinataContent": data
    	}
	headers = {
        "pinata_api_key": "af9600d75aec06ac35ae",
        "pinata_secret_api_key": "f4283784c4195bd20b5fab6eb160ade5d8351bd4dd66ee0ce0ceda61ea728d06",
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
	if cid.startswith("http"):
        	cid = cid.split("/")[-1]
	url = f"https://ipfs.io/ipfs/{cid}"
	response = requests.get(url)
	assert isinstance(data,dict), f"get_from_ipfs should return a dict"
	return data
