from apps.accounts.models import User, UserKey
from apps.trade.models import SpotOrder, SpotTakeProfit
from apps.trade.utils.common import make_spot_exchange, get_symbol_last_price

import ccxt
import logging
from django.utils import timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
def quick_close_spot_position(order: SpotOrder, user: User):
    try:
        # Validate order type
        if not order.is_spot:
            logger.error(f"Order {order.id} is not a spot order")
            return False

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = make_spot_exchange(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        symbol = order.symbol
        full_quantity = float(order.final_quantity or order.order_quantity)
        quantity = full_quantity

        # Determine side (opposite of original order)
        side = "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"

        # Cancel protective stop by stored id if present
        try:
            if order.stop_loss_order_id:
                try:
                    exchange.cancel_order(id=order.stop_loss_order_id, symbol=symbol)
                    order.stop_loss_status = SpotOrder.TradeStatus.CANCELLED
                except Exception:
                    pass
        except Exception:
            pass

        # Cancel any still-open limit TP orders so their reserved quantity is
        # freed rather than left resting on the exchange after we mark this
        # order CLOSED locally.
        open_tps = SpotTakeProfit.objects.filter(
            order=order, status=SpotTakeProfit.TradeStatus.POSITION
        )
        for tp in open_tps:
            try:
                exchange.cancel_order(id=tp.tp_order_id, symbol=symbol)
            except Exception:
                pass
        open_tps.update(status=SpotTakeProfit.TradeStatus.CANCELLED)

        # Get current market price for validation
        current_price = get_symbol_last_price(exchange, symbol)
        if not current_price:
            logger.error(f"Could not fetch current price for {symbol}")
            return False

        # Any TPs that already filled banked their own realized PnL on their
        # own leg quantity — only what's left of the position should be sold
        # here, and only that remaining quantity's PnL should be computed
        # below, on top of (not instead of) what those legs realized.
        entry_price = float(order.entry_price)
        closed_tps = SpotTakeProfit.objects.filter(
            order=order, status=SpotTakeProfit.TradeStatus.CLOSED
        )
        realized_tp_qty = sum((float(c.quantity) for c in closed_tps), 0.0)
        realized_tp_pnl = sum(
            (
                (float(c.price) - entry_price) * float(c.quantity)
                if order.direction == SpotOrder.TradeDirection.LONG
                else (entry_price - float(c.price)) * float(c.quantity)
                for c in closed_tps
            ),
            0.0,
        )
        quantity = full_quantity - realized_tp_qty
        if quantity <= 0:
            quantity = full_quantity

        # Some TPs may already have filled before this close (reducing what's
        # actually held) — cap by the real free balance rather than assuming
        # the full remaining quantity is still there.
        base_currency = symbol.split("/")[0]
        try:
            free_base = float(exchange.fetch_balance()["free"].get(base_currency, 0))
            if free_base > 0:
                quantity = min(quantity, free_base)
        except Exception:
            pass

        # Check minimum order requirements
        market = exchange.market(symbol)
        min_amount = float(market["limits"]["amount"]["min"])
        if quantity < min_amount:
            logger.info(
                f"Spot order {order.id} has no remaining quantity to close "
                f"({quantity} < min {min_amount} for {symbol}); likely fully filled via TP."
            )
            order.status = SpotOrder.TradeStatus.CLOSED
            order.closed_at = timezone.now()
            order.save()
            return True

        # Execute closing order
        close_order = exchange.create_order(
            symbol=symbol, type="market", side=side, amount=quantity
        )

        # Update order status and details
        order.exit_price = close_order["average"]
        order.status = SpotOrder.TradeStatus.CLOSED
        order.closed_at = timezone.now()

        # Calculate PNL for the leg closed just now, then add whatever was
        # already realized by earlier TP fills.
        close_leg_value_entry = entry_price * quantity
        close_leg_value_exit = float(close_order["average"]) * quantity

        if order.direction == SpotOrder.TradeDirection.LONG:
            close_leg_pnl = close_leg_value_exit - close_leg_value_entry
        else:
            close_leg_pnl = close_leg_value_entry - close_leg_value_exit
        order.pnl = realized_tp_pnl + close_leg_pnl

        # Percentage denominator is always the ORIGINAL full position's entry
        # value, not just what's left to close now.
        full_entry_value = entry_price * full_quantity
        order.pnl_percentage = (
            (float(order.pnl) / full_entry_value) * 100 if full_entry_value else 0
        )

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
