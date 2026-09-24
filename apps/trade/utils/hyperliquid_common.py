import ccxt
import logging

from django.core.cache import cache
from django.db import models
from apps.accounts.models import IPAddress

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# HyperLiquid quotes everything in USDC. A webhook symbol arrives Binance-style
# (base+quote concatenated, e.g. "SUIUSDC" or "SUIUSDT") — strip whichever
# quote suffix is present to recover the base coin before rebuilding a
# HyperLiquid unified symbol.
_KNOWN_QUOTE_SUFFIXES = ("USDC", "USDT", "USD")


_FULL_MARKETS_CACHE_KEY = "hyperliquid:full_markets"
_FULL_MARKETS_CACHE_TTL = 300  # seconds


def _get_cached_hyperliquid_markets():
    """The full parsed HyperLiquid market catalog (the same shape as
    exchange.markets after load_markets()), cached for a few minutes.

    HyperLiquid's own API responds in well under a second, but ccxt's
    client-side parsing of its ~800-market catalog into unified market
    objects takes ~10+ seconds of pure CPU time — call this what and
    where you like, that cost was being paid FRESH on every single order
    placement, close, and refresh (make_hyperliquid_exchange() builds a
    brand new exchange instance per call). Injecting this cached catalog
    via exchange.set_markets() instead of exchange.load_markets() skips
    that re-parse entirely; set_markets() just re-indexes already-unified
    data, which takes milliseconds regardless of catalog size.
    """
    cached = cache.get(_FULL_MARKETS_CACHE_KEY)
    if cached is not None:
        return cached
    loader = ccxt.hyperliquid({"enableRateLimit": True, "timeout": 15000})
    loader.load_markets()
    cache.set(_FULL_MARKETS_CACHE_KEY, loader.markets, _FULL_MARKETS_CACHE_TTL)
    return loader.markets


def make_hyperliquid_exchange(wallet_address: str, private_key: str):
    """Create a CCXT HyperLiquid exchange instance with markets loaded.

    `wallet_address` is the master account address (used to read balances/
    positions); `private_key` is the API/agent wallet's private key, which
    must already be approved as an agent of that master account on
    HyperLiquid — signatures made with it are attributed to the master
    account automatically, no separate "acting as" field is needed.
    """
    ip_obj = IPAddress.objects.filter(is_active=True).order_by("usage_count").first()
    proxies = {}
    if ip_obj:
        proxies = {
            "http": ip_obj.http_proxy,
            "https": ip_obj.http_proxy,
        }
        ip_obj.usage_count = models.F("usage_count") + 1
        ip_obj.save(update_fields=["usage_count", "last_used"])
    exchange = ccxt.hyperliquid(
        {
            "walletAddress": wallet_address,
            "privateKey": private_key,
            "timeout": 30000,
            "enableRateLimit": True,
            "proxies": proxies,
        }
    )
    try:
        exchange.set_markets(_get_cached_hyperliquid_markets())
    except Exception as e:
        logger.warning(f"Could not use cached HyperLiquid markets, loading fresh: {e}")
        exchange.load_markets()
    return exchange


def to_hyperliquid_symbol(symbol: str, market: str) -> str:
    """Translate a plain concatenated webhook symbol (e.g. "SUIUSDC") into
    HyperLiquid's unified ccxt symbol: "SUI/USDC:USDC" for a perpetual swap,
    "SUI/USDC" for spot.
    """
    base = symbol.split("/")[0] if "/" in symbol else symbol
    for quote in _KNOWN_QUOTE_SUFFIXES:
        if base.endswith(quote) and len(base) > len(quote):
            base = base[: -len(quote)]
            break
    if market == "futures":
        return f"{base}/USDC:USDC"
    return f"{base}/USDC"


def get_hyperliquid_last_price(exchange, symbol):
    """A fast current-price lookup for a HyperLiquid symbol.

    Deliberately not using ccxt's generic fetch_ticker() (as
    apps.trade.utils.common.get_symbol_last_price does for Binance): for
    HyperLiquid, fetch_ticker is 'emulated' via fetch_tickers(), which
    unconditionally re-fetches AND re-parses the exchange's entire
    ~800-market catalog on every single call — the same expensive parse
    load_markets() just did (see _get_cached_hyperliquid_markets above).
    The lightweight `allMids` info endpoint returns every coin's mid price
    in one small, sub-second request instead. Returns None (not False,
    unlike get_symbol_last_price) on failure — equally falsy for the
    `if not current_price:` checks callers already use.
    """
    try:
        market = exchange.market(symbol)
        coin = market["baseName"] if market.get("swap") else market["id"]
        response = exchange.publicPostInfo({"type": "allMids"})
        mid = (response or {}).get(coin)
        return float(mid) if mid is not None else None
    except Exception as e:
        logger.warning(f"Could not fetch HyperLiquid last price for {symbol}: {e}")
        return None


def create_trigger_order(
    exchange,
    symbol,
    side,
    quantity,
    trigger_price,
    *,
    take_profit: bool,
    reduce_only: bool = True,
):
    """Places a stop-loss or take-profit trigger order that executes as a
    market order once triggered — HyperLiquid's equivalent of Binance's
    STOP_MARKET / TAKE_PROFIT_MARKET conditional orders. HyperLiquid trigger
    orders are regular orders (no separate algo-order endpoint), so they can
    be fetched/cancelled via the normal fetch_order/cancel_order calls.
    """
    trigger_price_p = float(exchange.priceToPrecision(symbol, trigger_price))
    quantity_p = float(exchange.amountToPrecision(symbol, quantity))
    params = {"reduceOnly": reduce_only}
    if take_profit:
        params["takeProfitPrice"] = trigger_price_p
    else:
        params["stopLossPrice"] = trigger_price_p

    logger.info(
        f"[hl_trigger] Placing {'TP' if take_profit else 'SL'} for {symbol}: "
        f"side={side}, qty={quantity_p}, trigger={trigger_price_p}"
    )
    order = exchange.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=quantity_p,
        price=trigger_price_p,
        params=params,
    )
    order["id"] = str(order.get("id") or "")
    return order


def fetch_hl_fill_data(exchange, symbol, limit=100):
    """Maps order id -> {"avg_price", "closed_pnl", "fee"}, from this
    symbol's recent fill/trade history.

    HyperLiquid's order-status endpoint (what fetch_hl_order_status queries)
    reports no fill price once an order is queried after the fact — only
    the order's own preset limit price (which, for a triggered SL/TP, is
    that trigger price shifted by the slippage tolerance baked in at
    creation time — e.g. ~5% below/above target, never the real exit price)
    — and no PnL or fee figure at all. The real execution price, and
    HyperLiquid's own reported realized PnL for the trade (its `closedPnl`
    field — the same number their own fill history and third-party
    trackers like HyperDash display), only come back in the create-order
    response at the instant an order fills, or from the account's fill
    history afterward. This is that fill history, fetched once per refresh
    cycle and shared across every order-status lookup that needs it.
    """
    try:
        trades = exchange.fetch_my_trades(symbol, None, limit)
    except Exception as e:
        logger.warning(f"Could not fetch HyperLiquid fills for {symbol}: {e}")
        return {}
    totals = {}
    for t in trades:
        oid = str(t.get("order") or "")
        price = float(t.get("price") or 0)
        amount = float(t.get("amount") or 0)
        if not oid or amount <= 0:
            continue
        info = t.get("info") or {}
        closed_pnl = float(info.get("closedPnl") or 0)
        fee = float((t.get("fee") or {}).get("cost") or 0)
        agg = totals.setdefault(oid, {"qty": 0.0, "notional": 0.0, "closed_pnl": 0.0, "fee": 0.0})
        agg["qty"] += amount
        agg["notional"] += price * amount
        agg["closed_pnl"] += closed_pnl
        agg["fee"] += fee
    return {
        oid: {
            "avg_price": (agg["notional"] / agg["qty"]) if agg["qty"] else 0.0,
            "closed_pnl": agg["closed_pnl"],
            "fee": agg["fee"],
        }
        for oid, agg in totals.items()
        if agg["qty"] > 0
    }


def fetch_hl_order_status(exchange, symbol, order_id, fill_data=None):
    """Fetches a HyperLiquid order's fill status. Returns a normalized dict
    {"filled": bool, "average": float, "closed_pnl": float | None, "fee":
    float} or None if not found / a local placeholder id (e.g. "DISABLED-...").

    `fill_data`, if given, is a fetch_hl_fill_data() result used to recover
    the real execution price, HyperLiquid's own realized PnL, and the real
    fee for a filled order. Without it, a filled SL/TP's "average" would
    fall back to the order's own preset limit price (offset by ~5% for
    guaranteed execution — it can misrepresent a profitable exit as a
    loss), and "closed_pnl" stays None so callers fall back to computing
    PnL from price/qty themselves.
    """
    if not order_id or str(order_id).startswith(("DISABLED-", "RESIZE-FAILED-")):
        return None
    try:
        info = exchange.fetch_order(id=order_id, symbol=symbol)
    except Exception as e:
        logger.warning(f"Could not fetch HyperLiquid order {order_id}: {e}")
        return None
    filled_amt = float(info.get("filled") or 0)
    is_filled = info.get("status") == "closed" and filled_amt > 0
    average = float(info.get("average") or 0)
    closed_pnl = None
    fee = 0.0
    fd = fill_data.get(str(order_id)) if fill_data else None
    if fd:
        if not average:
            average = fd["avg_price"]
        closed_pnl = fd["closed_pnl"]
        fee = fd["fee"]
    if is_filled and not average:
        # Real fill price unreachable (e.g. fill has aged out of history) —
        # fall back to the order's own limit price rather than 0, which
        # would zero out this leg's PnL entirely.
        average = float(info.get("price") or 0)
    return {
        "filled": is_filled,
        "average": average,
        "closed_pnl": closed_pnl,
        "fee": fee,
        "raw": info,
    }


def cancel_hl_order(exchange, symbol, order_id):
    """Cancels a HyperLiquid order by id. Not fatal if it already
    triggered/filled/expired."""
    if not order_id or str(order_id).startswith(("DISABLED-", "RESIZE-FAILED-")):
        return None
    try:
        return exchange.cancel_order(id=order_id, symbol=symbol)
    except Exception as e:
        logger.warning(f"Could not cancel HyperLiquid order {order_id} for {symbol}: {e}")
        return None


_PUBLIC_MARKETS_CACHE_KEY = "hyperliquid:public_markets"
_PUBLIC_MARKETS_CACHE_TTL = 300  # seconds


def get_public_hyperliquid_markets(force_refresh=False):
    """Public (credential-less) HyperLiquid market metadata, cached for a
    few minutes so callers don't hit HyperLiquid's API on every request —
    used to resolve the exact "coin" identifier HyperLiquid's realtime
    websocket feed expects for a given unified symbol (see
    get_hyperliquid_ws_coin below).

    Only loads "spot"/"swap" markets — NOT "hip3" (builder-deployed DEX
    markets, which none of our users trade). ccxt's default load_markets()
    also fetches hip3 markets, which costs an extra "perpDexs" call plus up
    to 10 more sequential HTTP round-trips (one per DEX); chained together,
    those can individually stay under the per-request timeout yet still add
    up to more than gunicorn's worker timeout, killing the worker mid
    request (this took down /history/ in prod once — see git history).
    """
    if not force_refresh:
        cached = cache.get(_PUBLIC_MARKETS_CACHE_KEY)
        if cached is not None:
            return cached

    exchange = ccxt.hyperliquid({
        "enableRateLimit": True,
        "timeout": 5000,
        "options": {"fetchMarkets": {"types": ["spot", "swap"]}},
    })
    exchange.load_markets()
    markets = {
        symbol: {
            "id": m.get("id"),
            "baseName": m.get("baseName"),
            "swap": bool(m.get("swap")),
        }
        for symbol, m in exchange.markets.items()
    }
    cache.set(_PUBLIC_MARKETS_CACHE_KEY, markets, _PUBLIC_MARKETS_CACHE_TTL)
    return markets


def get_hyperliquid_ws_coin(hl_symbol: str) -> str:
    """The "coin" identifier HyperLiquid's `activeAssetCtx` websocket
    subscription expects for this unified symbol — the ticker for a perp
    (e.g. "SUI"), or an internal id for a spot pair (e.g. "@142"; with one
    legacy exception, "PURR/USDC", whose id is its own name, since it
    predates HyperLiquid's general spot-listing framework).

    This (not the slower, batched "allMids" subscription) is what the
    live-PnL script in history.html subscribes with — see
    https://github.com/ccxt/ccxt/issues/27475 for why allMids lags.

    Returns "" if the symbol can't be resolved (market metadata unreachable,
    or no such market), so callers can skip the live-PnL subscription for
    that row rather than using a wrong/guessed id.
    """
    try:
        markets = get_public_hyperliquid_markets()
        m = markets.get(hl_symbol)
        if not m:
            return ""
        return m["baseName"] if m["swap"] else m["id"]
    except Exception as e:
        logger.warning(f"Could not resolve HyperLiquid ws coin id for {hl_symbol}: {e}")
        return ""
