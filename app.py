import csv
import io
import math
import time
from flask import Flask, render_template, jsonify, request, Response
import requests
from web3 import Web3

app = Flask(__name__)

# --------------------------------------------------------------------
#                           Config
# --------------------------------------------------------------------

INFURA_URL = "https://mainnet.infura.io/v3/a191fe8059fd463588656c4d34dae905"
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# ABI with slot0, liquidity, token0, token1, and fee()
POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24",   "name": "tick",         "type": "int24"},
            {"internalType": "uint16",  "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8",   "name": "feeProtocol",  "type": "uint8"},
            {"internalType": "bool",    "name": "unlocked",     "type": "bool"}
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
    },
    {
        "constant": True,
        "inputs": [],
        "name": "fee",
        "outputs": [
            {"internalType": "uint24", "name": "", "type": "uint24"}
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

# Average for Ethereum mainnet is ~6500 blocks/day
BLOCKS_PER_DAY_ESTIMATE = 6500

# Mapping for subgraph "first" argument based on day range
SUBGRAPH_RANGE_MAP = {
    "1d": 1,
    "1w": 7,
    "1m": 30,
    "1y": 365
}

# On-chain sampling intervals
ONCHAIN_SAMPLING_CONFIG = {
    "1d": {"points": 24, "interval_blocks": int(BLOCKS_PER_DAY_ESTIMATE / 24)},
    "1w": {"points": 14, "interval_blocks": int((BLOCKS_PER_DAY_ESTIMATE * 7) / 14)},
    "1m": {"points": 30, "interval_blocks": int((BLOCKS_PER_DAY_ESTIMATE * 30) / 30)},
    "1y": {"points": 52, "interval_blocks": int((BLOCKS_PER_DAY_ESTIMATE * 365) / 52)}
}

# --------------------------------------------------------------------
#               Helper / Utility Functions
# --------------------------------------------------------------------

def sqrtPriceX96_to_price(sqrtPriceX96, decimal0=8, decimal1=8):
    """
    Converts Uniswap's sqrtPriceX96 value to a price.
    price = (sqrtPriceX96 / 2**96)**2
    """
    factor = 2 ** 96
    sqrt_price = sqrtPriceX96 / factor
    price = sqrt_price ** 2
    if decimal0 != decimal1:
        price *= 10 ** (decimal0 - decimal1)
    return price

def fetch_pool_metadata(pool_contract):
    """
    Fetch token0, token1, and fee tier from the pool.
    Returns a dict with token addresses, names, symbols, and feeTierDecimal.
    """
    try:
        t0_address = pool_contract.functions.token0().call()
        t1_address = pool_contract.functions.token1().call()
        fee_raw = pool_contract.functions.fee().call()  # uint24 value
        fee_decimal = fee_raw / 1_000_000  # e.g., 3000 -> 0.003
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
            "feeTierDecimal": fee_decimal,
        }
    except Exception as e:
        print("Error fetching pool metadata:", e)
        return {}

def find_liquidity_bands(prices, coverage_list=[0.5, 0.75, 0.8]):
    """
    Return dict of coverage fraction -> (lowestPrice, highestPrice)
    for a minimal band that encloses the specified fraction of data.
    """
    results = {}
    if not prices:
        return results
    sorted_prices = sorted(prices)
    n = len(sorted_prices)
    for cov in coverage_list:
        window_size = int(cov * n)
        if window_size < 1:
            results[cov] = (None, None)
            continue
        min_band_width = float('inf')
        band_low, band_high = None, None
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

def choose_best_band(coverage_bands):
    """
    From the coverage_bands dict (e.g. {0.5: (low, high), 0.75: (low, high), ...}),
    choose the band with the narrowest range.
    Returns (coverage_fraction, (range_low, range_high)).
    """
    if not coverage_bands:
        return (None, (None, None))
    best_cov = None
    best_range = None
    smallest_width = float('inf')
    for cov, (low_val, high_val) in coverage_bands.items():
        if low_val is None or high_val is None:
            continue
        width = high_val - low_val
        if width < smallest_width:
            smallest_width = width
            best_cov = cov
            best_range = (low_val, high_val)
    return (best_cov, best_range)

def estimate_yield(volumeUSD, coverage_fraction, fee_tier=0.003):
    """
    A toy daily yield estimate:
    daily_yield = volumeUSD * fee_tier * coverage_fraction
    """
    if volumeUSD is None or coverage_fraction is None:
        return 0
    return volumeUSD * fee_tier * coverage_fraction

def calculate_impermanent_loss(ratio):
    """
    Given a price change ratio (new price / old price),
    returns the impermanent loss (as a decimal, negative value).
    """
    il = (2 * math.sqrt(ratio) / (ratio + 1)) - 1
    return il

def compute_dynamic_impermanent_loss(subgraph_data):
    """
    Compute a dynamic impermanent loss based on subgraph token0Price history.
    Uses the earliest and the latest subgraph token0Price values to compute a ratio.
    Returns a dict with keys "price_increase", "price_decrease", and "ratio".
    For simplicity, both price_increase and price_decrease are set to the same dynamic IL.
    """
    if subgraph_data and len(subgraph_data) >= 2:
        earliest_price = subgraph_data[0]["token0Price"]
        latest_price = subgraph_data[-1]["token0Price"]
        ratio = latest_price / earliest_price if earliest_price != 0 else 1
        il_value = calculate_impermanent_loss(ratio)
        return {"price_increase": il_value, "price_decrease": il_value, "ratio": ratio}
    return {"price_increase": 0, "price_decrease": 0, "ratio": 1}

# --------------------------------------------------------------------
#                          Data Fetchers
# --------------------------------------------------------------------

def get_price_history(range_key="1w", pool_contract=None):
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
        for entry in data:
            entry["date"] = int(entry["date"]) * 1000
            entry["token0Price"] = float(entry["token0Price"])
            entry["token1Price"] = float(entry["token1Price"])
            entry["volumeUSD"] = float(entry["volumeUSD"])
            entry["tvlUSD"] = float(entry["tvlUSD"])
        return data[::-1]
    except Exception as e:
        print("Error fetching subgraph data:", e)
        return []

# --------------------------------------------------------------------
#                           Flask Routes
# --------------------------------------------------------------------

@app.route('/price-history')
def price_history():
    range_key = request.args.get("range", "1w")
    pool_input = request.args.get("pool", "")
    try:
        pool_address_checksum = web3.to_checksum_address(pool_input)
    except ValueError:
        return jsonify([])
    dynamic_contract = web3.eth.contract(address=pool_address_checksum, abi=POOL_ABI)
    data = get_price_history(range_key, dynamic_contract)
    return jsonify(data)

@app.route('/')
def index():
    # 1) Determine pool address
    pool_input = request.args.get("pool", "")
    if not pool_input:
        pool_input = "0xe8f7c89C5eFa061e340f2d2F206EC78FD8f7e124"
    try:
        pool_address_checksum = web3.to_checksum_address(pool_input)
    except ValueError:
        pool_address_checksum = web3.to_checksum_address("0xe8f7c89C5eFa061e340f2d2F206EC78FD8f7e124")
    pool_address_lower = pool_address_checksum.lower()

    # 2) Get range key
    range_key = request.args.get("range", "1w")

    # 3) Create dynamic pool contract instance
    pool_contract_dynamic = web3.eth.contract(address=pool_address_checksum, abi=POOL_ABI)

    # 4) Fetch pool metadata (token info & fee tier)
    tokens_info = fetch_pool_metadata(pool_contract_dynamic)

    # 5) On-chain data
    on_chain_data = get_price_history(range_key, pool_contract_dynamic)

    # 6) Subgraph data
    subgraph_data = fetch_subgraph_data(range_key, pool_address_lower)

    # 7) Compute coverage bands from subgraph's token0Price
    subgraph_prices = [d["token0Price"] for d in subgraph_data if d["token0Price"] > 0]
    coverage_bands = find_liquidity_bands(subgraph_prices, [0.5, 0.75, 0.8])

    # 8) Choose best band and compute yield
    best_cov, best_band_range = choose_best_band(coverage_bands)
    suggested_low, suggested_high = best_band_range if best_band_range else (None, None)
    daily_volume = None
    if subgraph_data:
        daily_volume = subgraph_data[-1]["volumeUSD"]
    expected_yield = None
    if best_cov and daily_volume:
        fee_decimal = tokens_info.get("feeTierDecimal", 0.003)
        expected_yield = estimate_yield(
            volumeUSD=daily_volume,
            coverage_fraction=best_cov,
            fee_tier=fee_decimal
        )

    # 9) Compute impermanent loss dynamically from subgraph data
    impermanent_loss_data = compute_dynamic_impermanent_loss(subgraph_data)

    return render_template(
        "index.html",
        range_key=range_key,
        on_chain_data=on_chain_data,
        subgraph_data=subgraph_data,
        tokens_info=tokens_info,
        coverage_bands=coverage_bands,
        best_cov=best_cov,
        suggested_low=suggested_low,
        suggested_high=suggested_high,
        expected_yield=expected_yield,
        impermanent_loss=impermanent_loss_data
    )

# ------------------------------ CSV EXPORTS ------------------------------

@app.route("/export-subgraph-csv")
def export_subgraph_csv():
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
    writer.writerow(["date", "token0Price", "token1Price", "volumeUSD", "tvlUSD"])
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
