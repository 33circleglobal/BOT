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


def get_symbol_current_market_price(symbol, exchange):
    try:
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker["last"]
        return current_price
    except Exception as e:
        logger.error(f"Error fetching ticker for {symbol}: {e}")
        return False


def set_margin_mode(exchange, symbol, margin_mode):
    try:
        info = exchange.fapiprivatev2_get_positionrisk(
            {"symbol": exchange.market_id(symbol)}
        )[0]
        current_margin_mode = info["marginType"]
        if margin_mode != current_margin_mode:
            exchange.fapiPrivatePostMarginType(
                {
                    "symbol": exchange.market_id(symbol),
                    "marginType": margin_mode.upper(),
                }
            )
    except Exception as e:
        logger.error(f"Error setting margin mode for {symbol}: {e}")
        return False


def create_binance_future_order(side, symbol, user):
    try:
        margin_mode = "isolated"
        side = side
        position = 20
        leverage = 10

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key._api_key, api_secret=user_binance_key._api_secret
        )

        current_price_of_symbol = get_symbol_current_market_price(
            symbol=symbol, exchange=exchange
        )
        balance = exchange.fetch_balance()
        user_balance = balance["free"]["USDT"]

        user_usable_balance = (user_balance * position / 100) * leverage
        quantity = user_usable_balance / current_price_of_symbol
        quantity = exchange.amountToPrecision(symbol, quantity)

        set_margin_mode(exchange, symbol, margin_mode)

        order = exchange.create_order(
            symbol=symbol, side=side, type="market", amount=quantity
        )

        position_direction = (
            Order.TradeDirection.LONG if side == "buy" else Order.TradeDirection.SHORT
        )

        Order.objects.create(
            order_id=order["id"],
            entry_price=order["average"],
            direction=position_direction,
            order_quantity=quantity,
            fee=order["fee"],
            symbol=symbol,
        )

        return print(f"Order created successfully for user {user.username}")

    except Exception as e:
        e
