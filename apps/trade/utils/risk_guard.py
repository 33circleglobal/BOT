"""Pre-open risk checks: max positions, max long/short, one-position-per-symbol.

These are fixed bot-wide rules (not yet user-configurable — allocation % is
intentionally left untouched here since it isn't fixed yet).
"""

from apps.trade.models import FutureOrder, SpotOrder

FUTURES_MAX_POSITIONS = 7
FUTURES_MAX_LONG = 4
FUTURES_MAX_SHORT = 4
FUTURES_ONE_POSITION_PER_SYMBOL = True

SPOT_MAX_POSITIONS = 7
SPOT_ONE_POSITION_PER_SYMBOL = True


def can_open_futures_position(user, symbol, direction):
    """Returns (allowed: bool, reason: str)."""
    open_positions = FutureOrder.objects.filter(
        user=user, status=FutureOrder.TradeStatus.POSITION
    )

    if FUTURES_ONE_POSITION_PER_SYMBOL and open_positions.filter(symbol=symbol).exists():
        return False, f"{symbol} already has an open futures position (one-position-per-symbol)"

    if open_positions.count() >= FUTURES_MAX_POSITIONS:
        return False, f"Max futures positions reached ({FUTURES_MAX_POSITIONS})"

    if direction == FutureOrder.TradeDirection.LONG:
        long_count = open_positions.filter(
            direction=FutureOrder.TradeDirection.LONG
        ).count()
        if long_count >= FUTURES_MAX_LONG:
            return False, f"Max LONG futures positions reached ({FUTURES_MAX_LONG})"
    else:
        short_count = open_positions.filter(
            direction=FutureOrder.TradeDirection.SHORT
        ).count()
        if short_count >= FUTURES_MAX_SHORT:
            return False, f"Max SHORT futures positions reached ({FUTURES_MAX_SHORT})"

    return True, ""


def can_open_spot_position(user, symbol):
    """Returns (allowed: bool, reason: str)."""
    open_positions = SpotOrder.objects.filter(
        user=user, status=SpotOrder.TradeStatus.POSITION
    )

    if SPOT_ONE_POSITION_PER_SYMBOL and open_positions.filter(symbol=symbol).exists():
        return False, f"{symbol} already has an open spot position (one-position-per-symbol)"

    if open_positions.count() >= SPOT_MAX_POSITIONS:
        return False, f"Max spot positions reached ({SPOT_MAX_POSITIONS})"

    return True, ""
