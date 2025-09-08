from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder

import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_connection_with_ccxt(api_key, api_secret):
    exchange = ccxt.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
        }
    )
    exchange.load_markets()
    return exchange


def quick_close_position(order: FutureOrder, user: User):
    try:
        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        # Use decrypted credentials via properties
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )
        symbol = order.symbol
        quantity = order.order_quantity
        side = "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"

        close_order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=quantity,
            params={"reduceOnly": True},
        )

        cancel_sl_order = exchange.cancel_order(
            id=order.stop_loss_order_id,
            symbol=symbol,
        )

        order.tp_order_id = close_order["id"]
        order.tp_price = close_order["average"]
        order.status = FutureOrder.TradeStatus.CLOSED
        order.tp_status = FutureOrder.TradeStatus.CLOSED
        # Fee info may be missing depending on exchange response
        fee = close_order.get("fee") or {}
        fee_cost = fee.get("cost", 0)
        order.tp_fee = fee_cost
        order.total_fee = float(order.total_fee) + float(fee_cost)
        order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED

        if order.direction == FutureOrder.TradeDirection.LONG:
            entry_price = float(order.entry_price)
            exit_price = float(close_order["average"])
            pnl = float(exit_price - entry_price) * float(quantity)
            order.pnl = pnl
        else:
            entry_price = float(order.entry_price)
            exit_price = float(close_order["average"])
            pnl = float(exit_price - entry_price) * float(quantity)
            order.pnl = pnl
        order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
        order.save()
        return print(f"Order closed successfully for user {user.username}")
    except Exception as e:
        logger.error(
            f"Error closing futures position for {user.username}: {e}", exc_info=True
        )
