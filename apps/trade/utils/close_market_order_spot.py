from apps.accounts.models import User, UserKey
from apps.trade.models import SpotOrder

import ccxt
import logging
from django.utils import timezone

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


def quick_close_spot_position(order: SpotOrder, user: User):
    try:
        # Validate order type
        if not order.is_spot:
            logger.error(f"Order {order.id} is not a spot order")
            return False

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        symbol = order.symbol
        quantity = float(order.final_quantity)

        # Determine side (opposite of original order)
        side = "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"

        # Get current market price for validation
        current_price = get_symbol_current_market_price(symbol, exchange)
        if not current_price:
            logger.error(f"Could not fetch current price for {symbol}")
            return False

        # Check minimum order requirements
        market = exchange.market(symbol)
        min_amount = float(market["limits"]["amount"]["min"])
        if quantity < min_amount:
            logger.error(
                f"Order quantity {quantity} is below minimum {min_amount} for {symbol}"
            )
            return False

        # Execute closing order
        close_order = exchange.create_order(
            symbol=symbol, type="market", side=side, amount=quantity
        )

        # Update order status and details
        order.exit_price = close_order["average"]
        order.status = SpotOrder.TradeStatus.CLOSED
        order.closed_at = timezone.now()

        # Calculate PNL
        entry_value = float(order.entry_price) * quantity
        exit_value = float(close_order["average"]) * quantity

        if order.direction == SpotOrder.TradeDirection.LONG:
            order.pnl = exit_value - entry_value
        else:
            order.pnl = entry_value - exit_value

        # Calculate PNL percentage
        order.pnl_percentage = (float(order.pnl) / entry_value) * 100

        # Update fee information
        if "fee" in close_order:
            order.exit_fee = float(close_order["fee"]["cost"])
            order.exit_fee_currency = close_order["fee"]["currency"]
            order.total_fee = float(order.total_fee) + float(close_order["fee"]["cost"])

        order.save()

        logger.info(
            f"Spot position closed for {user.username}: "
            f"{quantity} {symbol} at {close_order['average']}. "
            f"PNL: {order.pnl:.2f} {order.exit_fee_currency}"
        )
        return True

    except ccxt.InsufficientFunds as e:
        logger.error(
            f"Insufficient funds to close position for {user.username}: {str(e)}"
        )
        return False
    except ccxt.InvalidOrder as e:
        logger.error(f"Invalid order parameters for {user.username}: {str(e)}")
        return False
    except Exception as e:
        logger.error(
            f"Error closing spot position for {user.username}: {str(e)}", exc_info=True
        )
        return False
