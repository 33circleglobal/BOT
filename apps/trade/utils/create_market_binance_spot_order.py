from apps.accounts.models import User, UserKey
from apps.trade.models import SpotOrder
from apps.trade.utils.common import (
    make_spot_exchange,
    get_symbol_last_price,
    compute_default_sl,
)

import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_binance_spot_order(side: str, symbol: str, user: User, *, sl: float | None = None):
    try:
        side = side.lower()
        position = 100

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = make_spot_exchange(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        # Get market info for minimum order quantity check
        market = exchange.market(symbol)
        current_price_of_symbol = get_symbol_last_price(exchange, symbol)

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

        order = exchange.create_order(symbol=symbol, side=side, type="market", amount=quantity)

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

        created = SpotOrder.objects.create(
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

        # Attempt to place a protective stop-loss order for spot
        try:
            if side == "buy":
                sl_side = "sell"
                amount = float(created.final_quantity) or float(quantity)
                sl_price = float(sl) if sl else compute_default_sl(order["average"], side)
                # Binance spot typically uses STOP_LOSS_LIMIT; set price equal to stopPrice (tight limit)
                params = {"stopPrice": float(exchange.priceToPrecision(symbol, sl_price))}
                limit_price = params["stopPrice"]  # simple approximation
                # Place protective stop as limit stop to increase acceptance on spot markets
                sl_created = exchange.create_order(
                    symbol=symbol,
                    side=sl_side,
                    type="STOP_LOSS_LIMIT",
                    amount=float(exchange.amountToPrecision(symbol, amount)),
                    price=float(exchange.priceToPrecision(symbol, limit_price)),
                    params=params,
                )
                try:
                    created.stop_loss_order_id = sl_created.get("id", "")
                    created.stop_loss_price = params["stopPrice"]
                    created.stop_loss_status = SpotOrder.TradeStatus.POSITION
                    created.save(update_fields=["stop_loss_order_id", "stop_loss_price", "stop_loss_status"])
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to place spot stop-loss for {symbol}: {e}")

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
        logger.error(f"Error creating spot order for {user.username}: {str(e)}", exc_info=True)
        return False
