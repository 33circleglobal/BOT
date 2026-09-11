"""Pre-open risk checks: max positions, max long/short, one-position-per-symbol.

Max-position/long/short limits are per-user and configurable from the UI
(see apps.trade.models.TradeSettings and the "Risk Settings" page). The
webhook's "update_futures_risk" action (see apps.trade.views) overwrites
these fields bot-wide for every user based on the current market regime.
The one-position-per-symbol rules remain fixed bot-wide behavior.
"""

from apps.trade.models import FutureOrder, SpotOrder, TradeSettings

FUTURES_ONE_POSITION_PER_SYMBOL = True
SPOT_ONE_POSITION_PER_SYMBOL = True


def can_open_futures_position(user, symbol, direction):
    """Returns (allowed: bool, reason: str)."""
    limits = TradeSettings.get_for_user(user)
    open_positions = FutureOrder.objects.filter(
        user=user, status=FutureOrder.TradeStatus.POSITION
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


def can_open_spot_position(user, symbol):
    """Returns (allowed: bool, reason: str)."""
    limits = TradeSettings.get_for_user(user)
    open_positions = SpotOrder.objects.filter(
        user=user, status=SpotOrder.TradeStatus.POSITION
    )

    if SPOT_ONE_POSITION_PER_SYMBOL and open_positions.filter(symbol=symbol).exists():
        return False, f"{symbol} already has an open spot position (one-position-per-symbol)"

    if open_positions.count() >= limits.spot_max_positions:
        return False, f"Max spot positions reached ({limits.spot_max_positions})"

    return True, ""
