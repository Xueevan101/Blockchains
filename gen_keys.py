from web3 import Web3
from eth_account.messages import encode_defunct
import eth_account
import os

def sign_message(challenge, filename="secret_key.txt"):
    """
    challenge - byte string
    filename - filename of the file that contains your account secret key
    To pass the tests, your signature must verify, and the account you use
    must have testnet funds on both the bsc and avalanche test networks.
    """
    # This code will read your "sk.txt" file
    # If the file is empty, it will raise an exception
    with open(filename, "r") as f:
        key = f.readlines()
    assert len(key) > 0, "Your account secret_key.txt is empty"

    private_key = key[0].strip()
    w3 = Web3()
    
    message = encode_defunct(challenge)
    # TODO recover your account information for your private key and sign the given challenge
    # Use the code from the signatures assignment to sign the given challenge
    my_account = w3.eth.account.from_key(private_key)
    signed_message = my_account.sign_message(message)
    crypto_addr = my_account.address
    assert eth_account.Account.recover_message(message, signature=signed_message.signature) == crypto_addr, "Didn't work"
    return signed_message, crypto_addr



if __name__ == "__main__":
    for i in range(4):
        challenge = os.urandom(64)
        sig, addr = sign_message(challenge=challenge)
        print(f"Address: {addr}\nSignature: {sig}\n")
