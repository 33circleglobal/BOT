import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def make_spot_exchange(api_key: str, api_secret: str):
    """Create a CCXT Binance spot exchange instance and load markets."""
    exchange = ccxt.binance({"apiKey": api_key, "secret": api_secret})
    exchange.load_markets()
    return exchange


def make_futures_exchange(api_key: str, api_secret: str):
    """Create a CCXT Binance USDM futures exchange instance and load markets."""
    exchange = ccxt.binanceusdm({"apiKey": api_key, "secret": api_secret})
    exchange.load_markets()
    return exchange


def get_symbol_last_price(exchange, symbol: str):
    """Fetch last traded price for a symbol. Returns float or False on error."""
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker.get("last")
    except Exception as e:
        logger.error(f"Error fetching ticker for {symbol}: {e}")
        return False


def compute_default_sl(entry_price: float, side: str, pct: float = 0.01) -> float:
    """Compute default stop-loss price at +/- pct from entry based on side."""
    side = side.lower()
    if side == "buy":
        return round(float(entry_price) * (1 - pct), 4)
    else:
        return round(float(entry_price) * (1 + pct), 4)


def opposite_side(side: str) -> str:
    return "sell" if side.lower() == "buy" else "buy"

