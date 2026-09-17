from apps.accounts.models import User, UserHyperLiquidKey
from apps.trade.models import FutureOrder, FutureTakeProfit
from apps.trade.utils.hyperliquid_common import (
    make_hyperliquid_exchange,
    to_hyperliquid_symbol,
    cancel_hl_order,
    get_hyperliquid_last_price,
)

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def quick_close_hyperliquid_position(order: FutureOrder, user: User):
    try:
        hl_key = UserHyperLiquidKey.objects.get(user=user, is_active=True)
        exchange = make_hyperliquid_exchange(
            wallet_address=hl_key.get_master_wallet_address(),
            private_key=hl_key.get_api_private_key(),
        )
        symbol = to_hyperliquid_symbol(order.symbol, "futures")
        full_quantity = order.order_quantity
        side = "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"

        closed_tps = FutureTakeProfit.objects.filter(
            order=order, status=FutureTakeProfit.TradeStatus.CLOSED
        )
        realized_tp_qty = sum((float(c.quantity) for c in closed_tps), 0.0)
        entry_price = float(order.entry_price)
        realized_tp_pnl = sum(
            (
                (float(c.price) - entry_price) * float(c.quantity)
                if order.direction == FutureOrder.TradeDirection.LONG
                else (entry_price - float(c.price)) * float(c.quantity)
                for c in closed_tps
            ),
            0.0,
        )
        quantity = float(full_quantity) - realized_tp_qty
        if quantity <= 0:
            quantity = float(full_quantity)

        cancel_hl_order(exchange, symbol, order.stop_loss_order_id)

        open_tps = FutureTakeProfit.objects.filter(
            order=order, status=FutureTakeProfit.TradeStatus.POSITION
        )
        for tp in open_tps:
            cancel_hl_order(exchange, symbol, tp.tp_order_id)
        open_tps.update(status=FutureTakeProfit.TradeStatus.CANCELLED)

        current_price = get_hyperliquid_last_price(exchange, symbol)
        close_order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=quantity,
            price=current_price,
            params={"reduceOnly": True},
        )
        # Deliberately not re-fetching via fetch_order() afterward — see the
        # comment in create_market_hyperliquid_order.py; it would replace
        # this correct average with a wrong/misleading one.
        exit_avg = float(close_order.get("average") or current_price or 0)

        order.status = FutureOrder.TradeStatus.CLOSED
        order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED

        if order.direction == FutureOrder.TradeDirection.LONG:
            close_leg_pnl = float(exit_avg - entry_price) * quantity
        else:
            close_leg_pnl = float(entry_price - exit_avg) * quantity
        order.pnl = realized_tp_pnl + close_leg_pnl

        leverage = float(order.leverage or 1)
        notional = entry_price * float(full_quantity)
        margin = notional / leverage if leverage else notional
        order.pnl_percentage = (float(order.pnl) / margin) * 100 if margin else 0

        order.save()
        logger.info(f"HyperLiquid order closed successfully for user {user.username}")
        return True
    except Exception as e:
        logger.error(
            f"Error closing HyperLiquid futures position for {user.username}: {e}",
            exc_info=True,
        )
        return False
