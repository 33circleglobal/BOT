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


def apply_leverage(exchange, symbol, leverage):
    try:
        exchange.fapiprivate_post_leverage(
            {
                "symbol": exchange.market_id(symbol),
                "leverage": leverage,
            }
        )
    except Exception as e:
        logger.error(f"Error applying leverage for {symbol}: {e}")
        return False


def create_binance_future_order(side, symbol, user):
    try:
        margin_mode = "isolated"
        side = side
        position = 25
        leverage = 5

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        print(user_binance_key.api_key, user_binance_key.api_secret)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
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
        apply_leverage(exchange, symbol, leverage)

        order = exchange.create_order(
            symbol=symbol, side=side, type="market", amount=quantity
        )

        inverted_side = "sell" if side == "buy" else "buy"

        stop_price = (
            round(float(order["average"]) - float(order["average"]) * 0.01, 4)
            if side == "buy"
            else round(float(order["average"]) + float(order["average"]) * 0.01, 4)
        )

        sl_order = exchange.create_order(
            symbol=symbol,
            side=inverted_side,
            type="STOP_MARKET",
            amount=quantity,
            params={"stopPrice": stop_price, "reduceOnly": True},
        )

        position_direction = (
            FutureOrder.TradeDirection.LONG
            if side == "buy"
            else FutureOrder.TradeDirection.SHORT
        )

        fee = order.get("fee") or {}
        entry_fee = fee.get("cost", 0)
        entry_fee_currency = fee.get("currency", "USDT")
        total_fee = fee.get("cost", 0)

        stop_loss_price = (
            sl_order.get("price")
            or sl_order.get("stopPrice")
            or sl_order.get("triggerPrice")
        )

        FutureOrder.objects.create(
            order_id=order["id"],
            symbol=symbol,
            direction=position_direction,
            leverage=5,
            order_quantity=quantity,
            entry_price=order["average"],
            entry_fee=entry_fee,
            entry_fee_currency=entry_fee_currency,
            total_fee=total_fee,
            stop_loss_order_id=sl_order["id"],
            stop_loss_price=stop_loss_price,
            # placeholder; will be set when position is closed
            tp_order_id="",
            user=user,
        )

        return print(f"Order created successfully for user {user.username}")

    except Exception as e:
        print("Caught exception:", e)
        e
