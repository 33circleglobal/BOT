from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder, FutureTakeProfit
from django.utils import timezone
from decimal import Decimal

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


def refresh_orders():
    orders = FutureOrder.objects.filter(status=FutureOrder.TradeStatus.POSITION)

    for order in orders:
        user = order.user
        quantity = float(order.order_quantity)
        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        # Check SL first
        if order.stop_loss_order_id:
            try:
                sl_info = exchange.fetch_order(id=order.stop_loss_order_id, symbol=order.symbol)
                if sl_info.get("remaining") == 0 and sl_info.get("status") == "closed":
                    order.stop_loss_price = sl_info.get("average") or sl_info.get("price")
                    order.status = FutureOrder.TradeStatus.CLOSED
                    order.stop_loss_status = FutureOrder.TradeStatus.CLOSED
                    fee = sl_info.get("fee") or {}
                    order.stop_loss_fee = float(fee.get("cost", 0))
                    order.total_fee = float(order.total_fee or 0) + float(fee.get("cost", 0))

                    entry_price = float(order.entry_price)
                    exit_price = float(order.stop_loss_price)
                    if order.direction == FutureOrder.TradeDirection.LONG:
                        order.pnl = (exit_price - entry_price) * quantity
                    else:
                        order.pnl = (entry_price - exit_price) * quantity
                    order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
                    order.closed_at = timezone.now()
                    order.save()
                    continue
            except Exception:
                pass

        # Legacy parent TP removed
        # Check multiple TPs children
        children = FutureTakeProfit.objects.filter(order=order)
        for child in children:
            if child.status == FutureTakeProfit.TradeStatus.CLOSED:
                continue
            try:
                info = exchange.fetch_order(id=child.tp_order_id, symbol=order.symbol)
                if info.get("remaining") == 0 and info.get("status") == "closed":
                    child.status = FutureTakeProfit.TradeStatus.CLOSED
                    fee = info.get("fee") or {}
                    child.fee = float(fee.get("cost", 0))
                    child.save()
                    order.total_fee = float(order.total_fee or 0) + float(child.fee)
                    # accumulate realized pnl on parent for partial fills
                    exit_avg = float(info.get("average") or info.get("price") or 0)
                    entry = float(order.entry_price)
                    qty_leg = float(child.quantity)
                    pnl_leg = (
                        (exit_avg - entry) * qty_leg
                        if order.direction == FutureOrder.TradeDirection.LONG
                        else (entry - exit_avg) * qty_leg
                    )
                    order.pnl = float(order.pnl or 0) + pnl_leg
                    notional = entry * float(order.order_quantity or 0)
                    if notional:
                        order.pnl_percentage = (float(order.pnl) / notional) * 100
                    order.save(update_fields=["total_fee", "pnl", "pnl_percentage"])
            except Exception:
                pass

        # If all TP children are closed, close parent order
        if children:
            closed_qty = sum(float(c.quantity) for c in children if c.status == FutureTakeProfit.TradeStatus.CLOSED)
            total_qty = float(order.order_quantity)
            if closed_qty >= total_qty and order.status == FutureOrder.TradeStatus.POSITION:
                order.status = FutureOrder.TradeStatus.CLOSED
                order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED
                order.closed_at = timezone.now()
                order.save()

        # Fallback: recompute realized pnl from closed children
        try:
            closed_children = list(FutureTakeProfit.objects.filter(order=order, status=FutureTakeProfit.TradeStatus.CLOSED))
            if closed_children:
                entry = Decimal(str(order.entry_price))
                total_qty = Decimal(str(order.order_quantity)) if order.order_quantity else Decimal("0")
                realized = Decimal("0")
                for child in closed_children:
                    exit_avg = Decimal(str(child.price))
                    qty_leg = Decimal(str(child.quantity))
                    if order.direction == FutureOrder.TradeDirection.LONG:
                        realized += (exit_avg - entry) * qty_leg
                    else:
                        realized += (entry - exit_avg) * qty_leg
                order.pnl = realized
                if total_qty and entry:
                    notional = entry * total_qty
                    order.pnl_percentage = (realized / notional) * Decimal("100")
                order.save(update_fields=["pnl", "pnl_percentage"])
        except Exception:
            pass
