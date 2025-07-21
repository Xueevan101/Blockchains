#!/bin/python
import hashlib
import os
import random


def mine_block(k, prev_hash, transactions):
    """
    k - Number of trailing zeros in the binary representation (integer)
    prev_hash - the hash of the previous block (bytes)
    transactions - list of strings (transaction data)

    Returns a nonce (bytes) such that:
    SHA256(prev_hash + transactions + nonce) has ≥ k trailing binary zeros
    """
    if not isinstance(k, int) or k < 0:
        print("mine_block expects positive integer")
        return b'\x00'

    tx_data = ''.join(transactions).encode('utf-8')

    nonce_int = 0
    while True:
        nonce = nonce_int.to_bytes(8, 'big')
        m = hashlib.sha256()
        m.update(prev_hash)     
        m.update(tx_data)      
        m.update(nonce)     
        hash_result = m.digest()
        bin_hash = bin(int.from_bytes(hash_result, 'big'))
        if bin_hash.endswith('0' * k):
            print(f"Found valid nonce after {nonce_int} attempts.")
            break

        nonce_int += 1

    assert isinstance(nonce, bytes), 'nonce should be of type bytes'
    return nonce


def get_random_lines(filename, quantity):
    """
    This is a helper function to get the quantity of lines ("transactions")
    as a list from the filename given. 
    Do not modify this function
    """
    lines = []
    with open(filename, 'r') as f:
        for line in f:
            lines.append(line.strip())

    random_lines = []
    for x in range(quantity):
        random_lines.append(lines[random.randint(0, quantity - 1)])
    return random_lines


if __name__ == '__main__':
    # This code will be helpful for your testing
    filename = "bitcoin_text.txt"
    num_lines = 10  # The number of "transactions" included in the block

    # The "difficulty" level. For our blocks this is the number of Least Significant Bits
    # that are 0s. For example, if diff = 5 then the last 5 bits of a valid block hash would be zeros
    # The grader will not exceed 20 bits of "difficulty" because larger values take to long
    diff = 20

    transactions = get_random_lines(filename, num_lines)
    nonce = mine_block(diff, transactions)
    print(nonce)
