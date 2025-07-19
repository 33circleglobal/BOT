from apps.accounts.models import User, UserKey
from apps.trade.models import Order

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


def quick_close_position(order: Order, user: User):
    try:
        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key._api_key, api_secret=user_binance_key._api_secret
        )
        symbol = order.symbol
        quantity = order.order_quantity
        side = "sell" if order.direction == Order.TradeDirection.LONG else "buy"

        close_order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=quantity,
            params={"reduceOnly": True},
        )

        order.exit_price = close_order["average"]
        order.status = Order.TradeStatus.CLOSED

        if order.direction == Order.TradeDirection.LONG:
            entry_price = float(order.entry_price)
            exit_price = float(close_order["average"])
            pnl = (exit_price - entry_price) * quantity
            order.pnl = pnl
        else:
            entry_price = float(order.entry_price)
            exit_price = float(close_order["average"])
            pnl = (entry_price - exit_price) * quantity
            order.pnl = pnl
        order.save()
        return print(f"Order closed successfully for user {user.username}")
    except Exception as e:
        e
