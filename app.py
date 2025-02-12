import csv
import io
from flask import Flask, render_template, jsonify, request, Response
import requests
from web3 import Web3

app = Flask(__name__)

# --------------------------------------------------------------------
#                           config
# --------------------------------------------------------------------

INFURA_URL = "https://mainnet.infura.io/v3/a191fe8059fd463588656c4d34dae905"
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# ABI with the slot0, liquidity, token0, and token1 functions
POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160","name": "sqrtPriceX96","type": "uint160"},
            {"internalType": "int24", "name": "tick","type": "int24"},
            {"internalType": "uint16","name": "observationIndex","type": "uint16"},
            {"internalType": "uint16","name": "observationCardinality","type": "uint16"},
            {"internalType": "uint16","name": "observationCardinalityNext","type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol","type": "uint8"},
            {"internalType": "bool", "name": "unlocked","type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [
            {"internalType": "uint128","name": "","type": "uint128"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    }
]

# ERC20 ABI snippet for name() and symbol()
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    }
]

# Ethereum mainnet is ~6500 blocks per day
BLOCKS_PER_DAY_ESTIMATE = 6500

# Mapping for subgraph "first" argument based on day range
SUBGRAPH_RANGE_MAP = {
    "1d": 1,
    "1w": 7,
    "1m": 30,
    "1y": 365
}

# on-chain sampling intervals
ONCHAIN_SAMPLING_CONFIG = {
    "1d": {"points": 24, "interval_blocks": int(BLOCKS_PER_DAY_ESTIMATE / 24)},   # ~1 point/hour
    "1w": {"points": 14, "interval_blocks": int((BLOCKS_PER_DAY_ESTIMATE*7)/14)},# ~2 points/day
    "1m": {"points": 30, "interval_blocks": int((BLOCKS_PER_DAY_ESTIMATE*30)/30)},# 1 point/day
    "1y": {"points": 52, "interval_blocks": int((BLOCKS_PER_DAY_ESTIMATE*365)/52)}# 1 point/week
}

# --------------------------------------------------------------------
#                      Helper / Utility Functions
# --------------------------------------------------------------------

def sqrtPriceX96_to_price(sqrtPriceX96, decimal0=8, decimal1=8):
    """
    Converts Uniswap's sqrtPriceX96 value to a price.
    The formula is: price = (sqrtPriceX96 / 2**96)**2
    Optionally adjust for token decimals if they differ.
    """
    factor = 2 ** 96
    sqrt_price = sqrtPriceX96 / factor
    price = sqrt_price ** 2

    # Adjust for decimals if token0 and token1 have different decimals
    if decimal0 != decimal1:
        price *= 10 ** (decimal0 - decimal1)
    return price

def fetch_pool_token_metadata(pool_contract):
    """
    Given a dynamic pool contract, fetch token0/token1 addresses,
    then use ERC-20 calls to get each token's name/symbol.
    Returns a dict with:
      {
        "token0_address": ...,
        "token0_name": ...,
        "token0_symbol": ...,
        "token1_address": ...,
        "token1_name": ...,
        "token1_symbol": ...
      }
    """
    try:
        t0_address = pool_contract.functions.token0().call()
        t1_address = pool_contract.functions.token1().call()

        t0_contract = web3.eth.contract(address=t0_address, abi=ERC20_ABI)
        t1_contract = web3.eth.contract(address=t1_address, abi=ERC20_ABI)

        token0_name = t0_contract.functions.name().call()
        token0_symbol = t0_contract.functions.symbol().call()

        token1_name = t1_contract.functions.name().call()
        token1_symbol = t1_contract.functions.symbol().call()

        return {
            "token0_address": t0_address,
            "token0_name": token0_name,
            "token0_symbol": token0_symbol,
            "token1_address": t1_address,
            "token1_name": token1_name,
            "token1_symbol": token1_symbol,
        }
    except Exception as e:
        print("Error fetching token metadata:", e)
        return {}

def find_liquidity_bands(prices, coverage_list=[0.5, 0.75, 0.8]):
    """
    Given a list of historical prices, find the TIGHTEST band (lowest difference)
    that covers each coverage fraction in coverage_list.

    Returns dict:
      {
        0.5: (low_50pct, high_50pct),
        0.75: (low_75pct, high_75pct),
        0.8: (low_80pct, high_80pct)
      }
    """
    results = {}
    if not prices:
        return results

    sorted_prices = sorted(prices)
    n = len(sorted_prices)

    for cov in coverage_list:
        # # of data points needed to cover cov fraction
        window_size = int(cov * n)
        if window_size < 1:
            results[cov] = (None, None)
            continue

        min_band_width = float('inf')
        band_low, band_high = None, None

        # Slide a window of length = window_size across sorted_prices
        for i in range(0, n - window_size + 1):
            low_val = sorted_prices[i]
            high_val = sorted_prices[i + window_size - 1]
            width = high_val - low_val
            if width < min_band_width:
                min_band_width = width
                band_low = low_val
                band_high = high_val

        results[cov] = (band_low, band_high)

    return results

# --------------------------------------------------------------------
#                           Data Fetching
# --------------------------------------------------------------------

def get_price_history(range_key="1w", pool_contract=None):
    """
    Fetches historical on-chain price data by sampling older blocks.
    The range_key is one of: 1d, 1w, 1m, 1y.
    """
    if not pool_contract:
        return []

    if range_key not in ONCHAIN_SAMPLING_CONFIG:
        range_key = "1w"

    cfg = ONCHAIN_SAMPLING_CONFIG[range_key]
    num_points = cfg["points"]
    interval_blocks = cfg["interval_blocks"]

    price_history = []
    try:
        current_block = web3.eth.block_number
        for i in range(num_points):
            block = current_block - i * interval_blocks
            if block < 1:
                break

            block_data = web3.eth.get_block(block)
            timestamp = block_data.timestamp

            slot0 = pool_contract.functions.slot0().call(block_identifier=block)
            sqrtPriceX96 = slot0[0]

            price = sqrtPriceX96_to_price(sqrtPriceX96, decimal0=8, decimal1=8)

            price_history.append({
                "block": block,
                "timestamp": timestamp,
                "price": price
            })

        price_history = list(reversed(price_history))

    except Exception as e:
        print("Error fetching on-chain price history:", e)

    return price_history

def fetch_subgraph_data(range_key="1w", pool_lower_address=None):
    """
    Fetches historical daily data from a Uniswap V3 Subgraph for the given pool address.
    The range_key is one of: 1d, 1w, 1m, 1y.
    We'll request that many 'days' from the subgraph.
    """
    if not pool_lower_address:
        return []

    UNISWAP_SUBGRAPH_URL = (
        "https://gateway.thegraph.com/api/"
        "f8ee24992ea775a54043b630c0d38f92/subgraphs/id/5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
    )

    days_requested = SUBGRAPH_RANGE_MAP.get(range_key, 7)

    query = f"""
    {{
      poolDayDatas(
        first: {days_requested},
        orderBy: date,
        orderDirection: desc,
        where: {{ pool: "{pool_lower_address}" }}
      ) {{
        date
        token0Price
        token1Price
        volumeUSD
        tvlUSD
      }}
    }}
    """

    try:
        response = requests.post(UNISWAP_SUBGRAPH_URL, json={"query": query})
        if response.status_code != 200:
            print("Error querying subgraph:", response.status_code, response.text)
            return []

        res_json = response.json()
        if "errors" in res_json:
            print("Subgraph returned errors:", res_json["errors"])
            return []

        if "data" not in res_json:
            print("No 'data' field in subgraph response:", res_json)
            return []

        data = res_json["data"].get("poolDayDatas", [])
        if not isinstance(data, list):
            print("Unexpected 'poolDayDatas' structure:", data)
            return []

        # Convert date from seconds to ms, cast numeric fields
        for entry in data:
            entry["date"] = int(entry["date"]) * 1000
            entry["token0Price"] = float(entry["token0Price"])
            entry["token1Price"] = float(entry["token1Price"])
            entry["volumeUSD"]   = float(entry["volumeUSD"])
            entry["tvlUSD"]      = float(entry["tvlUSD"])

        return data[::-1]  # reverse to show oldest first
    except Exception as e:
        print("Error fetching subgraph data:", e)
        return []

# --------------------------------------------------------------------
#                          Flask Routes
# --------------------------------------------------------------------

@app.route('/price-history')
def price_history():
    """Return JSON of on-chain data, using ?range=1d / 1w / 1m / 1y & pool=?"""
    range_key = request.args.get("range", "1w")
    pool_input = request.args.get("pool", "")
    try:
        pool_address_checksum = web3.to_checksum_address(pool_input)
    except ValueError:
        # fallback or no pool
        return jsonify([])

    # Create the dynamic contract
    dynamic_contract = web3.eth.contract(address=pool_address_checksum, abi=POOL_ABI)
    data = get_price_history(range_key, dynamic_contract)
    return jsonify(data)

@app.route('/')
def index():
    """
    Main dashboard route.
    Usage: /?pool=<poolAddress>&range=1w
    """
    # 1) Figure out which pool address user wants
    pool_input = request.args.get("pool", "")
    if not pool_input:
        # If no user input, default to an example pool:
        pool_input = "0xe8f7c89C5eFa061e340f2d2F206EC78FD8f7e124"

    # 2) Convert to checksummed address
    try:
        pool_address_checksum = web3.to_checksum_address(pool_input)
    except ValueError:
        # fallback in case of invalid address
        pool_address_checksum = web3.to_checksum_address("0xe8f7c89C5eFa061e340f2d2F206EC78FD8f7e124")

    pool_address_lower = pool_address_checksum.lower()

    # 3) Determine which time range
    range_key = request.args.get("range", "1w")

    # 4) Dynamic contract instance
    pool_contract_dynamic = web3.eth.contract(address=pool_address_checksum, abi=POOL_ABI)

    # 5) Token metadata
    tokens_info = fetch_pool_token_metadata(pool_contract_dynamic)

    # 6) On-chain data
    on_chain_data = get_price_history(range_key, pool_contract_dynamic)

    # 7) Subgraph data
    subgraph_data = fetch_subgraph_data(range_key, pool_address_lower)

    # 8) Compute liquidity bands from subgraph prices
    subgraph_prices = [entry["token0Price"] for entry in subgraph_data if entry["token0Price"] > 0]
    coverage_bands = find_liquidity_bands(subgraph_prices, [0.5, 0.75, 0.8])

    # 9) Render
    return render_template(
        "index.html",
        range_key=range_key,
        on_chain_data=on_chain_data,
        subgraph_data=subgraph_data,
        tokens_info=tokens_info,
        coverage_bands=coverage_bands
    )


# ------------------------------ CSV EXPORTS ------------------------------

@app.route("/export-subgraph-csv")
def export_subgraph_csv():
    """
    Exports the subgraph data as a CSV file download.
    Usage: /export-subgraph-csv?range=1w&pool=...
    """
    range_key = request.args.get("range", "1w")
    pool_input = request.args.get("pool", "")
    try:
        pool_address_checksum = web3.to_checksum_address(pool_input)
        pool_address_lower = pool_address_checksum.lower()
    except ValueError:
        return Response("Invalid pool address", status=400)

    data = fetch_subgraph_data(range_key, pool_address_lower)

    output = io.StringIO()
    writer = csv.writer(output)

    # CSV header
    writer.writerow(["date", "token0Price", "token1Price", "volumeUSD", "tvlUSD"])

    # Rows
    for row in data:
        writer.writerow([
            row["date"],
            row["token0Price"],
            row["token1Price"],
            row["volumeUSD"],
            row["tvlUSD"]
        ])

    output.seek(0)

    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename=subgraph_data_{range_key}.csv"}
    )

@app.route("/export-onchain-csv")
def export_onchain_csv():
    """
    Exports the on-chain data as a CSV file download.
    Usage: /export-onchain-csv?range=1d&pool=...
    """
    range_key = request.args.get("range", "1w")
    pool_input = request.args.get("pool", "")
    try:
        pool_address_checksum = web3.to_checksum_address(pool_input)
    except ValueError:
        return Response("Invalid pool address", status=400)

    dynamic_contract = web3.eth.contract(address=pool_address_checksum, abi=POOL_ABI)
    data = get_price_history(range_key, dynamic_contract)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["block", "timestamp", "price"])
    for row in data:
        writer.writerow([row["block"], row["timestamp"], row["price"]])

    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename=onchain_data_{range_key}.csv"}
    )

# --------------------------------------------------------------------
#                             Main
# --------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
