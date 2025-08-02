from apps.accounts.models import User, UserKey
from apps.trade.models import SpotOrder

import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_connection_with_ccxt(api_key, api_secret):
    exchange = ccxt.binance(
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


def create_binance_spot_order(side, symbol, user):
    try:
        side = side.lower()
        position = 100

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        # Get market info for minimum order quantity check
        market = exchange.market(symbol)
        current_price_of_symbol = get_symbol_current_market_price(symbol, exchange)

        if not current_price_of_symbol:
            logger.error(f"Could not fetch current price for {symbol}")
            return False

        balance = exchange.fetch_balance()

        if side == "buy":
            # For buy orders, we use USDT balance
            user_balance = balance["free"]["USDT"]
            user_usable_balance = user_balance * position / 100
            quantity = user_usable_balance / current_price_of_symbol
        else:
            # For sell orders, we use the available crypto balance
            base_currency = symbol.split("/")[0]
            user_balance = balance["free"].get(base_currency, 0)
            quantity = user_balance * position / 100

        # Check minimum order quantity
        min_amount = float(market["limits"]["amount"]["min"])
        if quantity < min_amount:
            logger.error(
                f"Order quantity {quantity} is below minimum {min_amount} for {symbol}"
            )
            return False

        # Check minimum notional value (price * quantity)
        min_notional = float(market["limits"]["cost"]["min"])
        notional_value = quantity * current_price_of_symbol
        if notional_value < min_notional:
            logger.error(
                f"Order notional value {notional_value} is below minimum {min_notional} for {symbol}"
            )
            return False

        quantity = float(exchange.amountToPrecision(symbol, quantity))

        # Check if we have sufficient balance
        if quantity <= 0:
            logger.error(f"Insufficient balance for {symbol} {side} order")
            return False

        order = exchange.create_order(
            symbol=symbol, side=side, type="market", amount=quantity
        )

        # Extract detailed fee information
        fee_details = {
            "cost": order["fee"]["cost"],
            "currency": order["fee"]["currency"],
        }

        position_direction = (
            SpotOrder.TradeDirection.LONG
            if side == "buy"
            else SpotOrder.TradeDirection.SHORT
        )

        SpotOrder.objects.create(
            order_id=order["id"],
            entry_price=order["average"],
            direction=position_direction,
            order_quantity=quantity,
            final_quantity=float(quantity) - float(fee_details["cost"]),
            entry_fee=fee_details["cost"],
            entry_fee_currency=fee_details["currency"],
            total_fee=float(order["average"]) * fee_details["cost"],
            symbol=symbol,
            is_spot=True,
            total_cost=notional_value,
            exchange="binance",
            user=user,
        )

        logger.info(
            f"Spot {side} order created for {user.username}: "
            f"{quantity} {symbol} at {order['average']}. "
            f"Fee: {fee_details['cost']} {fee_details['currency']}"
        )
        return True

    except ccxt.InsufficientFunds as e:
        logger.error(f"Insufficient funds for {user.username}: {str(e)}")
        return False
    except ccxt.InvalidOrder as e:
        logger.error(f"Invalid order parameters for {user.username}: {str(e)}")
        return False
    except Exception as e:
        logger.error(
            f"Error creating spot order for {user.username}: {str(e)}", exc_info=True
        )
        return False
