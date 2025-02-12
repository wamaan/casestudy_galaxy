# app.py
from flask import Flask, render_template, jsonify
from web3 import Web3
import math

app = Flask(__name__)

# --- Configuration ---
# Replace with your Infura API key
INFURA_URL = "https://mainnet.infura.io/v3/a191fe8059fd463588656c4d34dae905"
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# Pool contract address for WBTC/cbBTC 0.01% pool
POOL_ADDRESS = web3.toChecksumAddress("0xe8f7c89C5eFa061e340f2d2F206EC78FD8f7e124")

# Minimal ABI with the slot0 and liquidity functions
POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [
            {"internalType": "uint128", "name": "", "type": "uint128"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

pool_contract = web3.eth.contract(address=POOL_ADDRESS, abi=POOL_ABI)

# --- Helper Function ---
def sqrtPriceX96_to_price(sqrtPriceX96, decimal0=8, decimal1=8):
    """
    Converts Uniswap's sqrtPriceX96 value to a price.
    The formula is: price = (sqrtPriceX96 / 2**96)**2
    Optionally adjust for token decimals.
    """
    factor = 2 ** 96
    sqrtPrice = sqrtPriceX96 / factor
    price = sqrtPrice ** 2
    if decimal0 != decimal1:
        price = price * (10 ** (decimal0 - decimal1))
    return price

# --- Fetch Historical Price Data ---
def get_price_history():
    """
    Sample historical price data fetching.
    We fetch every 5000 blocks over the last ~20 data points.
    Each data point includes: block number, timestamp, and computed price.
    """
    price_history = []
    try:
        current_block = web3.eth.block_number
        block_interval = 5000  # adjust interval for how far back you want to sample
        num_points = 20
        for i in range(num_points):
            block = current_block - i * block_interval
            block_data = web3.eth.get_block(block)
            timestamp = block_data.timestamp
            slot0 = pool_contract.functions.slot0().call(block_identifier=block)
            sqrtPriceX96 = slot0[0]
            price = sqrtPriceX96_to_price(sqrtPriceX96)
            price_history.append({
                "block": block,
                "timestamp": timestamp,
                "price": price
            })
        price_history = list(reversed(price_history))
    except Exception as e:
        print("Error fetching price history:", e)
    return price_history

# --- Flask Routes ---
@app.route('/price-history')
def price_history():
    data = get_price_history()
    return jsonify(data)

@app.route('/')
def index():
    data = get_price_history()  # fetch the historical data
    return render_template("index.html", price_data=data)

if __name__ == "__main__":
    # host set to 0.0.0.0 so that it is accessible in Docker
    app.run(host="0.0.0.0", port=5000, debug=True)

