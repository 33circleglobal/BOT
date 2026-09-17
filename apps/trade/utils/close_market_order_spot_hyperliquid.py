from apps.accounts.models import User, UserHyperLiquidKey
from apps.trade.models import SpotOrder, SpotTakeProfit
from apps.trade.utils.hyperliquid_common import (
    make_hyperliquid_exchange,
    to_hyperliquid_symbol,
    cancel_hl_order,
    get_hyperliquid_last_price,
)

import ccxt
import logging
from django.utils import timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def quick_close_hyperliquid_spot_position(order: SpotOrder, user: User):
    try:
        if not order.is_spot:
            logger.error(f"Order {order.id} is not a spot order")
            return False

        hl_key = UserHyperLiquidKey.objects.get(user=user, is_active=True)
        exchange = make_hyperliquid_exchange(
            wallet_address=hl_key.get_master_wallet_address(),
            private_key=hl_key.get_api_private_key(),
        )
        symbol = to_hyperliquid_symbol(order.symbol, "spot")

        full_quantity = float(order.final_quantity or order.order_quantity)
        quantity = full_quantity

        side = "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"

        if order.stop_loss_order_id:
            cancel_hl_order(exchange, symbol, order.stop_loss_order_id)
            order.stop_loss_status = SpotOrder.TradeStatus.CANCELLED

        open_tps = SpotTakeProfit.objects.filter(
            order=order, status=SpotTakeProfit.TradeStatus.POSITION
        )
        for tp in open_tps:
            cancel_hl_order(exchange, symbol, tp.tp_order_id)
        open_tps.update(status=SpotTakeProfit.TradeStatus.CANCELLED)

        if order.dca_status == SpotOrder.TradeStatus.POSITION and order.dca_order_id:
            cancel_hl_order(exchange, symbol, order.dca_order_id)
            order.dca_status = SpotOrder.TradeStatus.CANCELLED

        current_price = get_hyperliquid_last_price(exchange, symbol)
        if not current_price:
            logger.error(f"Could not fetch current price for {symbol}")
            return False

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

        base_currency = symbol.split("/")[0]
        try:
            free_base = float((exchange.fetch_balance(params={"type": "spot"})["free"] or {}).get(base_currency, 0))
            if free_base > 0:
                quantity = min(quantity, free_base)
        except Exception:
            pass

        market = exchange.market(symbol)
        min_amount = float(
            ((market.get("limits") or {}).get("amount") or {}).get("min") or 0
        )
        if quantity < min_amount:
            logger.info(
                f"HyperLiquid spot order {order.id} has no remaining quantity to close "
                f"({quantity} < min {min_amount} for {symbol}); likely fully filled via TP."
            )
            order.status = SpotOrder.TradeStatus.CLOSED
            order.closed_at = timezone.now()
            order.save()
            return True

        close_order = exchange.create_order(
            symbol=symbol, type="market", side=side, amount=quantity, price=current_price
        )
        # Deliberately not re-fetching via fetch_order() afterward — see the
        # comment in create_market_hyperliquid_order.py; it would replace
        # this correct average with a wrong/misleading one.
        exit_avg = float(close_order.get("average") or current_price)
        order.exit_price = exit_avg
        order.status = SpotOrder.TradeStatus.CLOSED
        order.closed_at = timezone.now()

        close_leg_value_entry = entry_price * quantity
        close_leg_value_exit = exit_avg * quantity

        if order.direction == SpotOrder.TradeDirection.LONG:
            close_leg_pnl = close_leg_value_exit - close_leg_value_entry
        else:
            close_leg_pnl = close_leg_value_entry - close_leg_value_exit
        order.pnl = realized_tp_pnl + close_leg_pnl

        full_entry_value = entry_price * full_quantity
        order.pnl_percentage = (
            (float(order.pnl) / full_entry_value) * 100 if full_entry_value else 0
        )

        order.save()

        logger.info(
            f"HyperLiquid spot position closed for {user.username}: "
            f"{quantity} {symbol} at {exit_avg}. PNL: {order.pnl:.2f} USDC"
        )
        return True

    except ccxt.InsufficientFunds as e:
        logger.error(
            f"Insufficient funds to close HyperLiquid position for {user.username}: {str(e)}"
        )
        return False
    except ccxt.InvalidOrder as e:
        logger.error(f"Invalid order parameters for {user.username}: {str(e)}")
        return False
    except Exception as e:
        logger.error(
            f"Error closing HyperLiquid spot position for {user.username}: {str(e)}",
            exc_info=True,
        )
        return False
