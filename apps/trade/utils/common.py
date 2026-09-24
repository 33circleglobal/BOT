import ccxt
import logging

from django.db import models
from apps.accounts.models import IPAddress

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def make_spot_exchange(api_key: str, api_secret: str):
    """Create a CCXT Binance spot exchange instance and load markets."""
    ip_obj = IPAddress.objects.filter(is_active=True).order_by("usage_count").first()
    proxies = {}
    if ip_obj:
        proxies = {
            "http": ip_obj.http_proxy,
            "https": ip_obj.http_proxy,
        }
        ip_obj.usage_count = models.F("usage_count") + 1
        ip_obj.save(update_fields=["usage_count", "last_used"])
    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "timeout": 30000,
            "enableRateLimit": True,
            "proxies": proxies,
            "options": {
                "adjustForTimeDifference": True,
            },
        }
    )
    exchange.load_markets()
    return exchange


def make_futures_exchange(api_key: str, api_secret: str):
    """Create a CCXT Binance USDM futures exchange instance and load markets."""
    ip_obj = IPAddress.objects.filter(is_active=True).order_by("usage_count").first()
    proxies = {}
    if ip_obj:
        proxies = {
            "http": ip_obj.http_proxy,
            "https": ip_obj.http_proxy,
        }
        ip_obj.usage_count = models.F("usage_count") + 1
        ip_obj.save(update_fields=["usage_count", "last_used"])
    exchange = ccxt.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "timeout": 30000,
            "enableRateLimit": True,
            "proxies": proxies,
            "options": {
                "adjustForTimeDifference": True,
            },
        }
    )
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


def compute_default_sl(entry_price: float, side: str, pct: float = 0.0025) -> float:
    """Compute default stop-loss price at +/- pct from entry based on side."""
    side = side.lower()
    if side == "buy":
        return round(float(entry_price) * (1 - pct), 4)
    else:
        return round(float(entry_price) * (1 + pct), 4)


def opposite_side(side: str) -> str:
    return "sell" if side.lower() == "buy" else "buy"


def split_spot_order_fees(
    order: dict, base_asset: str, quote_asset: str, fill_price: float
) -> tuple[float, float]:
    """Split a ccxt Binance order's commission(s) by what they actually cost.

    Binance's `executedQty` (ccxt: `order['filled']`) is always the *gross*
    base-asset quantity filled, before any commission is deducted — the fee
    is reported separately (`fills[].commission`/`commissionAsset`, unified
    by ccxt into `order['fees']`). Only when the commission asset is the
    *base* asset does it actually reduce the base-asset balance the buyer
    receives; a commission in the quote asset or in BNB is paid out of a
    different balance entirely and must not be subtracted from quantity.

    A single order can have multiple fills with different commission
    assets (e.g. BNB ran out mid-order), so this sums across
    `order['fees']` rather than assuming a single `order['fee']`.

    Returns (base_asset_fee, fee_value_in_quote):
    - base_asset_fee: total commission actually paid in the base asset —
      subtract this from executedQty to get the real sellable quantity.
    - fee_value_in_quote: every fee re-expressed in quote-currency terms
      (base-asset fees converted via fill_price, quote-asset fees taken
      as-is) for informational/accounting totals. Fees paid in an
      unrelated asset (e.g. BNB) can't be converted without an extra price
      lookup and are approximated as their raw cost.
    """
    base_asset = (base_asset or "").upper()
    quote_asset = (quote_asset or "").upper()
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])

    base_asset_fee = 0.0
    fee_value_in_quote = 0.0
    for f in fees:
        cost = float(f.get("cost") or 0)
        currency = (f.get("currency") or "").upper()
        if currency == base_asset:
            base_asset_fee += cost
            fee_value_in_quote += cost * float(fill_price or 0)
        elif currency == quote_asset:
            fee_value_in_quote += cost
        else:
            fee_value_in_quote += cost
    return base_asset_fee, fee_value_in_quote
