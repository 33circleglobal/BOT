from apps.accounts.models import UserKey
from apps.trade.models import FutureOrder, SpotOrder
from apps.trade.utils.common import make_futures_exchange, make_spot_exchange

import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def refresh_futures_order(order: FutureOrder) -> bool:
    """Check remote TP/SL orders and sync local order. Returns True if updated."""
    try:
        user_key = UserKey.objects.get(user=order.user, is_active=True)
        ex = make_futures_exchange(api_key=user_key.api_key, api_secret=user_key.api_secret)
        symbol = order.symbol
        qty = float(order.order_quantity)

        updated = False

        # Check SL
        if order.stop_loss_order_id:
            try:
                sl_info = ex.fetch_order(id=order.stop_loss_order_id, symbol=symbol)
                if sl_info.get("remaining") == 0 and sl_info.get("status") == "closed":
                    exit_avg = float(sl_info.get("average") or sl_info.get("price") or 0)
                    # mark as SL closed
                    order.status = FutureOrder.TradeStatus.CLOSED
                    order.stop_loss_price = exit_avg
                    order.stop_loss_status = FutureOrder.TradeStatus.CLOSED
                    order.tp_status = FutureOrder.TradeStatus.CANCELLED
                    fee = sl_info.get("fee") or {}
                    fee_cost = float(fee.get("cost", 0))
                    order.stop_loss_fee = fee_cost
                    order.total_fee = float(order.total_fee or 0) + fee_cost
                    if order.direction == FutureOrder.TradeDirection.LONG:
                        entry = float(order.entry_price)
                        order.pnl = (exit_avg - entry) * qty
                    else:
                        entry = float(order.entry_price)
                        order.pnl = (entry - exit_avg) * qty
                    order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
                    order.closed_at = timezone.now()
                    order.save()
                    updated = True
                    return updated
            except Exception:
                pass

        # Check TP
        if order.tp_order_id:
            try:
                tp_info = ex.fetch_order(id=order.tp_order_id, symbol=symbol)
                if tp_info.get("remaining") == 0 and tp_info.get("status") == "closed":
                    exit_avg = float(tp_info.get("average") or tp_info.get("price") or 0)
                    order.status = FutureOrder.TradeStatus.CLOSED
                    order.tp_price = exit_avg
                    order.tp_status = FutureOrder.TradeStatus.CLOSED
                    order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED
                    fee = tp_info.get("fee") or {}
                    fee_cost = float(fee.get("cost", 0))
                    order.tp_fee = fee_cost
                    order.total_fee = float(order.total_fee or 0) + fee_cost
                    if order.direction == FutureOrder.TradeDirection.LONG:
                        entry = float(order.entry_price)
                        order.pnl = (exit_avg - entry) * qty
                    else:
                        entry = float(order.entry_price)
                        order.pnl = (entry - exit_avg) * qty
                    order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
                    order.closed_at = timezone.now()
                    order.save()
                    updated = True
                    return updated
            except Exception:
                pass

        return updated
    except Exception as e:
        logger.error(f"Failed to refresh futures order {order.id}: {e}")
        return False


def refresh_spot_order(order: SpotOrder) -> bool:
    """Best-effort: check for opposite-side closed orders after creation and close locally.
    This is heuristic because we don't persist the stop order id for spot.
    """
    try:
        user_key = UserKey.objects.get(user=order.user, is_active=True)
        ex = make_spot_exchange(api_key=user_key.api_key, api_secret=user_key.api_secret)
        symbol = order.symbol
        side = "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"

        # Prefer precise check using stored SL order id
        if order.stop_loss_order_id:
            try:
                sl_info = ex.fetch_order(id=order.stop_loss_order_id, symbol=symbol)
                if sl_info.get("remaining") == 0 and sl_info.get("status") == "closed":
                    avg = float(sl_info.get("average") or sl_info.get("price") or 0)
                    qty = float(order.final_quantity or order.order_quantity)
                    order.exit_price = avg
                    order.status = SpotOrder.TradeStatus.CLOSED
                    order.stop_loss_status = SpotOrder.TradeStatus.CLOSED
                    order.closed_at = timezone.now()
                    entry_val = float(order.entry_price) * qty
                    exit_val = avg * qty
                    if order.direction == SpotOrder.TradeDirection.LONG:
                        order.pnl = exit_val - entry_val
                    else:
                        order.pnl = entry_val - exit_val
                    order.pnl_percentage = (float(order.pnl) / entry_val) * 100 if entry_val else 0
                    fee = sl_info.get("fee") or {}
                    if fee:
                        try:
                            order.exit_fee = float(fee.get("cost", 0))
                            order.exit_fee_currency = fee.get("currency", order.entry_fee_currency)
                            order.total_fee = float(order.total_fee or 0) + float(fee.get("cost", 0))
                        except Exception:
                            pass
                    order.save()
                    return True
            except Exception:
                pass

        # Fallback heuristic scan if no SL id present
        try:
            since = int(order.created_at.timestamp() * 1000)
            closed = ex.fetch_closed_orders(symbol, since)
            for co in closed or []:
                if (co.get("side") == side) and co.get("status") == "closed":
                    avg = float(co.get("average") or co.get("price") or 0)
                    qty = float(order.final_quantity or order.order_quantity)
                    order.exit_price = avg
                    order.status = SpotOrder.TradeStatus.CLOSED
                    order.closed_at = timezone.now()
                    entry_val = float(order.entry_price) * qty
                    exit_val = avg * qty
                    if order.direction == SpotOrder.TradeDirection.LONG:
                        order.pnl = exit_val - entry_val
                    else:
                        order.pnl = entry_val - exit_val
                    order.pnl_percentage = (float(order.pnl) / entry_val) * 100 if entry_val else 0
                    fee = co.get("fee") or {}
                    if fee:
                        try:
                            order.exit_fee = float(fee.get("cost", 0))
                            order.exit_fee_currency = fee.get("currency", order.entry_fee_currency)
                            order.total_fee = float(order.total_fee or 0) + float(fee.get("cost", 0))
                        except Exception:
                            pass
                    order.save()
                    return True
        except Exception:
            pass

        return False
    except Exception as e:
        logger.error(f"Failed to refresh spot order {order.id}: {e}")
        return False
