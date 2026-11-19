"""Pre-open risk checks: max positions, max long/short, one-position-per-symbol,
and market-data freshness.

Max-position/long/short limits are per-user and configurable from the UI
(see apps.trade.models.TradeSettings and the "Risk Settings" page). The
webhook's "update_market_risk" action (see apps.trade.views) overwrites
these fields bot-wide for every user based on the current market regime.
The one-position-per-symbol rules remain fixed bot-wide behavior.

Both `can_open_*` functions also refuse new positions when that webhook
feed has gone stale for a user who depends on it (see
`TradeSettings.is_market_data_stale`) — a dropped/failed webhook call must
never leave the bot silently trading on an outdated regime.

Both also accept an optional `exchange` filter so the position-count/
long/short checks can be scoped to one exchange family (e.g. only
HyperLiquid, or only Binance's BINANCE/BINANCE_FUTURES rows). Binance and
HyperLiquid are separate wallets/margin pools with independent risk, so a
busy Binance book must not consume a HyperLiquid signal's budget (or vice
versa) — see the production incidents where BTCUSDC/HYPEUSDC/PUMPUSDC
HyperLiquid futures signals, and a HyperLiquid spot signal, were silently
blocked by Binance-side positions alone exceeding the shared cap. Passing
no `exchange` preserves the old bot-wide pooled behavior.
"""

import logging

from apps.trade.models import FutureOrder, RiskDecisionLog, SpotOrder, TradeSettings
from apps.trade.utils import market_risk

logger = logging.getLogger(__name__)

FUTURES_ONE_POSITION_PER_SYMBOL = True
SPOT_ONE_POSITION_PER_SYMBOL = True


def _scope_by_exchange(queryset, exchange):
    if exchange is None:
        return queryset
    exchange_filter = exchange if isinstance(exchange, (list, tuple)) else (exchange,)
    return queryset.filter(exchange__in=exchange_filter)


def _is_hyperliquid(exchange):
    return isinstance(exchange, str) and exchange == "HYPERLIQUID"


def _hl_one_per_symbol(model, user, symbol):
    return model.objects.filter(
        user=user,
        symbol=symbol,
        exchange="HYPERLIQUID",
        status__in=[model.TradeStatus.POSITION, model.TradeStatus.OPEN],
    ).exists()


def _log_binance_block(user, market, symbol, direction, reason):
    """Audit a blocked Binance entry so the Risk Settings page can list it."""
    try:
        RiskDecisionLog.objects.create(
            user=user, exchange="BINANCE", market=market, symbol=(symbol or "")[:30],
            direction=(str(direction) if direction else "")[:10],
            allowed=False, reason=reason[:255],
        )
    except Exception:  # audit must never block or crash trading
        logger.exception("Failed to write Binance RiskDecisionLog")


def can_open_futures_position(user, symbol, direction, exchange=None):
    """Returns (allowed: bool, reason: str)."""
    if _is_hyperliquid(exchange):
        # HyperLiquid is governed by the 122Moon V3.2 controller snapshot
        # (freshness, member mode, profile caps), not the legacy Binance
        # regime webhook. Binance behavior below is unchanged.
        if FUTURES_ONE_POSITION_PER_SYMBOL and _hl_one_per_symbol(FutureOrder, user, symbol):
            return False, f"{symbol} already has an open futures position (one-position-per-symbol)"
        decision = market_risk.evaluate_entry(user, "futures", direction, symbol)
        if not decision.allowed:
            market_risk.log_decision(user, decision, symbol, direction)
        return decision.allowed, decision.reason

    allowed, reason = _binance_futures_check(user, symbol, direction, exchange)
    if not allowed:
        _log_binance_block(user, "futures", symbol, direction, reason)
    return allowed, reason


def _binance_futures_check(user, symbol, direction, exchange):
    limits = TradeSettings.get_for_user(user)

    if limits.is_market_data_stale():
        return False, (
            "Market-regime webhook data is stale (no update in over "
            f"{limits.max_market_data_age_minutes}m); refusing new futures positions"
        )

    # Resting limit entries (OPEN) count against the limits too, so pending
    # orders can't pile past max positions once they fill.
    open_positions = _scope_by_exchange(
        FutureOrder.objects.filter(
            user=user,
            status__in=[FutureOrder.TradeStatus.POSITION, FutureOrder.TradeStatus.OPEN],
        ),
        exchange,
    )

    if FUTURES_ONE_POSITION_PER_SYMBOL and open_positions.filter(symbol=symbol).exists():
        return False, f"{symbol} already has an open futures position (one-position-per-symbol)"

    if open_positions.count() >= limits.futures_max_positions:
        return False, f"Max futures positions reached ({limits.futures_max_positions})"

    if direction == FutureOrder.TradeDirection.LONG:
        long_count = open_positions.filter(
            direction=FutureOrder.TradeDirection.LONG
        ).count()
        if long_count >= limits.futures_max_long:
            return False, f"Max LONG futures positions reached ({limits.futures_max_long})"
    else:
        short_count = open_positions.filter(
            direction=FutureOrder.TradeDirection.SHORT
        ).count()
        if short_count >= limits.futures_max_short:
            return False, f"Max SHORT futures positions reached ({limits.futures_max_short})"

    return True, ""


def can_open_spot_position(user, symbol, exchange=None):
    """Returns (allowed: bool, reason: str)."""
    if _is_hyperliquid(exchange):
        if SPOT_ONE_POSITION_PER_SYMBOL and _hl_one_per_symbol(SpotOrder, user, symbol):
            return False, f"{symbol} already has an open spot position (one-position-per-symbol)"
        decision = market_risk.evaluate_entry(user, "spot", None, symbol)
        if not decision.allowed:
            market_risk.log_decision(user, decision, symbol)
        return decision.allowed, decision.reason

    allowed, reason = _binance_spot_check(user, symbol, exchange)
    if not allowed:
        _log_binance_block(user, "spot", symbol, "", reason)
    return allowed, reason


def _binance_spot_check(user, symbol, exchange):
    limits = TradeSettings.get_for_user(user)

    if limits.is_market_data_stale():
        return False, (
            "Market-regime webhook data is stale (no update in over "
            f"{limits.max_market_data_age_minutes}m); refusing new spot positions"
        )

    # Resting limit entries (OPEN) count too, same as futures.
    open_positions = _scope_by_exchange(
        SpotOrder.objects.filter(
            user=user,
            status__in=[SpotOrder.TradeStatus.POSITION, SpotOrder.TradeStatus.OPEN],
        ),
        exchange,
    )

    if SPOT_ONE_POSITION_PER_SYMBOL and open_positions.filter(symbol=symbol).exists():
        return False, f"{symbol} already has an open spot position (one-position-per-symbol)"

    if open_positions.count() >= limits.spot_max_positions:
        return False, f"Max spot positions reached ({limits.spot_max_positions})"

    return True, ""
