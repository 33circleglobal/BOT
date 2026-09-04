from apps.accounts.models import UserKey
from apps.trade.models import FutureOrder, SpotOrder, FutureTakeProfit
from apps.trade.utils.common import make_futures_exchange, make_spot_exchange

import logging
from django.utils import timezone
from decimal import Decimal

logger = logging.getLogger(__name__)


def fetch_algo_order_status(exchange, algo_id):
    """
    Queries a conditional (algo) order's status via GET /fapi/v1/algoOrder.
    Returns a normalized dict: {"filled": bool, "average": float, "fee": dict}
    or None if the order can't be found / algo_id is a placeholder.

    Note: unlike fetch_order, this endpoint takes algoId only (no symbol param),
    and "filled" for STOP_MARKET/TAKE_PROFIT_MARKET is inferred from
    actualOrderId + actualPrice being populated, since those order types
    execute as MARKET orders the instant they trigger (no partial-fill state
    to worry about, unlike a triggered LIMIT-type algo order).
    """
    if not algo_id or str(algo_id).startswith("DISABLED-"):
        return None
    try:
        info = exchange.fapiprivate_get_algoorder({"algoId": algo_id})
    except Exception as e:
        logger.warning(f"Could not fetch algo order {algo_id}: {e}")
        return None

    actual_order_id = info.get("actualOrderId") or ""
    actual_price = float(info.get("actualPrice") or 0)
    algo_status = info.get("algoStatus")

    filled = bool(actual_order_id) and actual_price > 0

    return {
        "filled": filled,
        "canceled": algo_status in ("CANCELED", "EXPIRED"),
        "average": actual_price,
        "actual_qty": float(info.get("actualQty") or 0),
        "raw": info,
    }


def refresh_futures_order(order: FutureOrder) -> bool:
    """Check remote TP/SL algo orders and sync local order. Returns True if updated."""
    try:
        user_key = UserKey.objects.get(user=order.user, is_active=True)
        ex = make_futures_exchange(
            api_key=user_key.api_key, api_secret=user_key.api_secret
        )
        qty = float(order.order_quantity)

        updated = False

        # Check SL only if it's active (POSITION)
        if (
            order.stop_loss_status == FutureOrder.TradeStatus.POSITION
            and order.stop_loss_order_id
        ):
            sl_info = fetch_algo_order_status(ex, order.stop_loss_order_id)
            if sl_info and sl_info["filled"]:
                exit_avg = sl_info["average"]
                # mark as SL closed
                order.status = FutureOrder.TradeStatus.CLOSED
                order.stop_loss_price = exit_avg
                order.stop_loss_status = FutureOrder.TradeStatus.CLOSED
                # Algo order response doesn't carry fee info directly;
                # fee is only reliably available via userTrades/fetch_my_trades.
                # Leaving fee untouched here rather than guessing 0.
                if order.direction == FutureOrder.TradeDirection.LONG:
                    entry = float(order.entry_price)
                    order.pnl = (exit_avg - entry) * qty
                else:
                    entry = float(order.entry_price)
                    order.pnl = (entry - exit_avg) * qty
                order.pnl_percentage = (
                    float(order.pnl) / float(order.entry_price)
                ) * 100
                order.closed_at = timezone.now()
                order.save()
                updated = True
                return updated

        # Legacy parent TP removed

        # Check multiple TPs (children). Do not close parent unless all filled.
        children = list(FutureTakeProfit.objects.filter(order=order))
        if children:
            any_updated = False
            for child in children:
                if child.status == FutureTakeProfit.TradeStatus.CLOSED:
                    continue
                tp_info = fetch_algo_order_status(ex, child.tp_order_id)
                if tp_info and tp_info["filled"]:
                    exit_avg = tp_info["average"]
                    child.status = FutureTakeProfit.TradeStatus.CLOSED
                    # Fee not directly available from algo order response;
                    # leaving child.fee as-is (0) rather than guessing.
                    child.save()
                    # Realized PnL accumulation for this filled TP leg
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
                    any_updated = True

            if any_updated:
                # If all TP children closed and no SL closed and not parent TP, we can consider parent closed if sum qty equals order qty.
                closed_qty = sum(
                    float(c.quantity)
                    for c in children
                    if c.status == FutureTakeProfit.TradeStatus.CLOSED
                )
                total_qty = float(order.order_quantity)
                if (
                    closed_qty >= total_qty
                    and order.status == FutureOrder.TradeStatus.POSITION
                ):
                    order.status = FutureOrder.TradeStatus.CLOSED
                    order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED
                    order.closed_at = timezone.now()
                order.save()
                return True

        # Fallback: ensure realized PnL reflects any already-closed TP legs
        try:
            closed_children = list(
                FutureTakeProfit.objects.filter(
                    order=order, status=FutureTakeProfit.TradeStatus.CLOSED
                )
            )
            if closed_children:
                entry = Decimal(str(order.entry_price))
                total_qty = (
                    Decimal(str(order.order_quantity))
                    if order.order_quantity
                    else Decimal("0")
                )
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
        return updated
    except Exception as e:
        logger.error(f"Failed to refresh futures order {order.id}: {e}")
        return False


def refresh_spot_order(order: SpotOrder) -> bool:
    """Best-effort: check for opposite-side closed orders after creation and close locally.
    This is heuristic because we don't persist the stop order id for spot.
    Spot orders are unaffected by the USDⓈ-M futures algoOrder migration —
    Binance Spot OCO/STOP_LOSS_LIMIT orders still go through the regular
    order endpoints, so this function is left as-is.
    """
    try:
        user_key = UserKey.objects.get(user=order.user, is_active=True)
        ex = make_spot_exchange(
            api_key=user_key.api_key, api_secret=user_key.api_secret
        )
        symbol = order.symbol
        side = "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"

        # Prefer precise check using stored SL order id when active
        if (
            order.stop_loss_status == SpotOrder.TradeStatus.POSITION
            and order.stop_loss_order_id
        ):
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
                    order.pnl_percentage = (
                        (float(order.pnl) / entry_val) * 100 if entry_val else 0
                    )
                    fee = sl_info.get("fee") or {}
                    if fee:
                        try:
                            order.exit_fee = float(fee.get("cost", 0))
                            order.exit_fee_currency = fee.get(
                                "currency", order.entry_fee_currency
                            )
                            order.total_fee = float(order.total_fee or 0) + float(
                                fee.get("cost", 0)
                            )
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
                    order.pnl_percentage = (
                        (float(order.pnl) / entry_val) * 100 if entry_val else 0
                    )
                    fee = co.get("fee") or {}
                    if fee:
                        try:
                            order.exit_fee = float(fee.get("cost", 0))
                            order.exit_fee_currency = fee.get(
                                "currency", order.entry_fee_currency
                            )
                            order.total_fee = float(order.total_fee or 0) + float(
                                fee.get("cost", 0)
                            )
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
