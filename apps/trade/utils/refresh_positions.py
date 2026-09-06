from apps.accounts.models import UserKey
from apps.trade.models import FutureOrder, SpotOrder, FutureTakeProfit, SpotTakeProfit
from apps.trade.utils.common import (
    make_futures_exchange,
    make_spot_exchange,
    opposite_side,
    compute_default_sl,
)
from apps.trade.utils.close_order import cancel_algo_order
from apps.trade.utils.create_market_order import create_algo_order

import logging
from django.utils import timezone
from decimal import Decimal
from uuid import uuid4

logger = logging.getLogger(__name__)

DUST = Decimal("0.00000001")


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


def _leg_pnl(direction, entry: Decimal, exit_price: Decimal, qty: Decimal) -> Decimal:
    if direction == FutureOrder.TradeDirection.LONG:
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def refresh_futures_order(order: FutureOrder) -> bool:
    """Sync one open futures position with the exchange.

    - Detects filled TP legs and shrinks the protective SL order's quantity
      to match whatever position size remains (e.g. a 10-qty position with
      TPs of 4/3/3: once the 4-qty TP fills, the SL is re-placed at qty 6).
    - Detects an SL fill: cancels any TPs still open (nothing left for them
      to reduce) and closes the parent order.
    - Detects all TPs filled (position fully closed without the SL ever
      triggering): closes the parent order and cancels the now-orphaned SL.
    - PnL is always the sum of every realized leg (each filled TP at its
      own exit price/qty, plus the SL fill on whatever quantity remained),
      not just a single full-size exit.
    """
    if order.status != FutureOrder.TradeStatus.POSITION:
        return False
    try:
        user_key = UserKey.objects.get(user=order.user, is_active=True)
        ex = make_futures_exchange(
            api_key=user_key.api_key, api_secret=user_key.api_secret
        )
        entry = Decimal(str(order.entry_price))
        total_qty = Decimal(str(order.order_quantity or 0))
        direction = order.direction
        entry_side = "buy" if direction == FutureOrder.TradeDirection.LONG else "sell"
        sl_side = opposite_side(entry_side)
        leverage = Decimal(str(order.leverage or 1))
        notional = entry * total_qty
        margin = (notional / leverage) if leverage else notional

        # --- 1. Reconcile any newly-filled TP legs ---------------------------
        children = list(FutureTakeProfit.objects.filter(order=order))
        newly_filled = False
        for child in children:
            if child.status != FutureTakeProfit.TradeStatus.POSITION:
                continue
            tp_info = fetch_algo_order_status(ex, child.tp_order_id)
            if tp_info and tp_info["filled"]:
                child.price = tp_info["average"]
                child.status = FutureTakeProfit.TradeStatus.CLOSED
                child.save()
                newly_filled = True

        # Recompute realized PnL from every closed TP leg — idempotent, so
        # this stays correct however many refresh cycles it takes.
        closed_children = [
            c for c in children if c.status == FutureTakeProfit.TradeStatus.CLOSED
        ]
        realized_tp_qty = sum((Decimal(str(c.quantity)) for c in closed_children), Decimal("0"))
        realized_tp_pnl = sum(
            (
                _leg_pnl(direction, entry, Decimal(str(c.price)), Decimal(str(c.quantity)))
                for c in closed_children
            ),
            Decimal("0"),
        )
        remaining_qty = total_qty - realized_tp_qty
        if remaining_qty < 0:
            remaining_qty = Decimal("0")

        # --- 2. All TPs filled: position fully closed without the SL --------
        if closed_children and remaining_qty <= DUST:
            if (
                order.stop_loss_status == FutureOrder.TradeStatus.POSITION
                and order.stop_loss_order_id
            ):
                cancel_algo_order(ex, order.symbol, order.stop_loss_order_id)
                order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED
            order.status = FutureOrder.TradeStatus.CLOSED
            order.pnl = realized_tp_pnl
            order.pnl_percentage = (
                (realized_tp_pnl / margin) * Decimal("100") if margin else Decimal("0")
            )
            order.closed_at = timezone.now()
            order.save()
            return True

        # --- 3. SL filled: close remaining qty at SL price, cancel other TPs -
        if (
            order.stop_loss_status == FutureOrder.TradeStatus.POSITION
            and order.stop_loss_order_id
        ):
            sl_info = fetch_algo_order_status(ex, order.stop_loss_order_id)
            if sl_info and sl_info["filled"]:
                exit_avg = Decimal(str(sl_info["average"]))
                sl_qty = remaining_qty if remaining_qty > 0 else total_qty
                sl_leg_pnl = _leg_pnl(direction, entry, exit_avg, sl_qty)

                still_open = [
                    c for c in children if c.status == FutureTakeProfit.TradeStatus.POSITION
                ]
                for c in still_open:
                    cancel_algo_order(ex, order.symbol, c.tp_order_id)
                    c.status = FutureTakeProfit.TradeStatus.CANCELLED
                    c.save()

                total_pnl = realized_tp_pnl + sl_leg_pnl
                order.status = FutureOrder.TradeStatus.CLOSED
                order.stop_loss_price = exit_avg
                order.stop_loss_status = FutureOrder.TradeStatus.CLOSED
                order.pnl = total_pnl
                order.pnl_percentage = (
                    (total_pnl / margin) * Decimal("100") if margin else Decimal("0")
                )
                order.closed_at = timezone.now()
                order.save()
                return True

        # --- 4. Partial TP fill, position still open: resize the SL qty -----
        # Also retries a previously-failed resize (stop_loss_status FAILED)
        # on every subsequent cycle so a transient API error self-heals
        # instead of leaving the remaining position unprotected.
        sl_needs_resize = newly_filled or order.stop_loss_status == FutureOrder.TradeStatus.FAILED
        has_live_sl = (
            order.stop_loss_status == FutureOrder.TradeStatus.POSITION
            and order.stop_loss_order_id
            and not str(order.stop_loss_order_id).startswith("DISABLED-")
        )
        if sl_needs_resize and remaining_qty > 0 and (has_live_sl or order.stop_loss_status == FutureOrder.TradeStatus.FAILED):
            breakeven = float(entry)
            already_at_breakeven = abs(float(order.stop_loss_price) - breakeven) < 1e-8
            if closed_children:
                # At least one TP has filled — protect the remainder at
                # breakeven instead of the original stop. Re-sent as-is
                # (no-op price-wise) if it's already sitting there; the SL
                # still gets cancelled/re-placed here because its quantity
                # must shrink to match remaining_qty.
                trigger_price = breakeven
            else:
                trigger_price = float(order.stop_loss_price)
                if trigger_price <= 0:
                    # Stored SL price is missing/invalid (e.g. stale data from
                    # before SL was enabled) — fall back to a sane default
                    # rather than sending an unusable near-zero trigger price.
                    trigger_price = compute_default_sl(float(entry), entry_side)
                    logger.warning(
                        f"[refresh] Order {order.id} had an invalid stop_loss_price "
                        f"({order.stop_loss_price}); using computed default SL "
                        f"{trigger_price} instead."
                    )
            if has_live_sl:
                cancel_algo_order(ex, order.symbol, order.stop_loss_order_id)
            new_qty = float(ex.amountToPrecision(order.symbol, float(remaining_qty)))
            try:
                new_sl = create_algo_order(
                    ex,
                    order.symbol,
                    sl_side,
                    "STOP_MARKET",
                    new_qty,
                    trigger_price=float(ex.priceToPrecision(order.symbol, trigger_price)),
                    reduce_only=True,
                )
                order.stop_loss_order_id = new_sl["id"]
                order.stop_loss_status = FutureOrder.TradeStatus.POSITION
                order.stop_loss_price = trigger_price
                if closed_children and not already_at_breakeven:
                    logger.info(
                        f"[sl] Moved SL to breakeven ({trigger_price}) for order "
                        f"{order.id} after a TP fill"
                    )
            except Exception as e:
                # Leave the position flagged as unprotected so the next
                # refresh cycle retries the resize rather than silently
                # tracking a cancelled/stale SL order id.
                order.stop_loss_status = FutureOrder.TradeStatus.FAILED
                order.stop_loss_order_id = f"RESIZE-FAILED-{uuid4()}"
                logger.error(
                    f"[refresh] Failed to resize SL for order {order.id} to "
                    f"qty {new_qty}: {e}",
                    exc_info=True,
                )

            order.pnl = realized_tp_pnl
            order.pnl_percentage = (
                (realized_tp_pnl / margin) * Decimal("100") if margin else Decimal("0")
            )
            order.save()
            return True

        if newly_filled:
            order.pnl = realized_tp_pnl
            order.pnl_percentage = (
                (realized_tp_pnl / margin) * Decimal("100") if margin else Decimal("0")
            )
            order.save()
            return True

        return False
    except Exception as e:
        logger.error(f"Failed to refresh futures order {order.id}: {e}")
        return False


def refresh_spot_order(order: SpotOrder) -> bool:
    """Sync one open spot position with the exchange.

    - Detects filled TP legs (plain LIMIT sell orders — spot has no algo
      endpoint like futures) and accumulates realized PnL from each.
    - Detects all TPs filled (position fully closed): closes the parent
      order and cancels the SL if one is somehow still live.
    - Detects an SL fill: cancels any TPs still open and closes the parent
      order, combining realized TP PnL with the SL leg's PnL.
    - If neither TPs nor a stored SL id explain the position closing, falls
      back to a heuristic scan of recent closed orders (legacy behavior for
      orders created before SL ids were persisted).

    Note: unlike futures, a filled spot TP doesn't trigger any SL move —
    TP-based spot signals are placed without a stop-loss in the first place
    (see create_binance_spot_order), so there's nothing to reprice here.
    """
    if order.status != SpotOrder.TradeStatus.POSITION:
        return False
    try:
        user_key = UserKey.objects.get(user=order.user, is_active=True)
        ex = make_spot_exchange(
            api_key=user_key.api_key, api_secret=user_key.api_secret
        )
        symbol = order.symbol
        side = "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"
        entry = Decimal(str(order.entry_price))
        total_qty = Decimal(str(order.final_quantity or order.order_quantity))
        direction = order.direction

        # --- 1. Reconcile any newly-filled TP legs ---------------------------
        children = list(SpotTakeProfit.objects.filter(order=order))
        newly_filled = False
        for child in children:
            if child.status != SpotTakeProfit.TradeStatus.POSITION:
                continue
            try:
                info = ex.fetch_order(id=child.tp_order_id, symbol=symbol)
            except Exception as e:
                logger.warning(f"Could not fetch spot TP order {child.tp_order_id}: {e}")
                continue
            status = info.get("status")
            if status == "closed" and float(info.get("filled") or 0) > 0:
                child.price = float(info.get("average") or info.get("price") or child.price)
                fee = info.get("fee") or {}
                if fee:
                    try:
                        child.fee = float(fee.get("cost", 0))
                    except Exception:
                        pass
                child.status = SpotTakeProfit.TradeStatus.CLOSED
                child.save()
                newly_filled = True
            elif status in ("canceled", "cancelled", "expired", "rejected"):
                # Cancelled/expired outside our control (e.g. manually on the
                # exchange) — stop tracking it as an open leg.
                child.status = SpotTakeProfit.TradeStatus.CANCELLED
                child.save()

        closed_children = [
            c for c in children if c.status == SpotTakeProfit.TradeStatus.CLOSED
        ]
        realized_tp_qty = sum((Decimal(str(c.quantity)) for c in closed_children), Decimal("0"))
        realized_tp_pnl = sum(
            (
                _leg_pnl(direction, entry, Decimal(str(c.price)), Decimal(str(c.quantity)))
                for c in closed_children
            ),
            Decimal("0"),
        )
        remaining_qty = total_qty - realized_tp_qty
        if remaining_qty < 0:
            remaining_qty = Decimal("0")
        entry_val = entry * total_qty

        # --- 2. All TPs filled: position fully closed without the SL --------
        if closed_children and remaining_qty <= DUST:
            if (
                order.stop_loss_status == SpotOrder.TradeStatus.POSITION
                and order.stop_loss_order_id
            ):
                try:
                    ex.cancel_order(id=order.stop_loss_order_id, symbol=symbol)
                except Exception:
                    pass
                order.stop_loss_status = SpotOrder.TradeStatus.CANCELLED
            order.exit_price = closed_children[-1].price
            order.status = SpotOrder.TradeStatus.CLOSED
            order.pnl = realized_tp_pnl
            order.pnl_percentage = (
                (realized_tp_pnl / entry_val) * Decimal("100") if entry_val else Decimal("0")
            )
            order.closed_at = timezone.now()
            order.save()
            return True

        # --- 3. SL filled: close remaining qty at SL price, cancel other TPs -
        if (
            order.stop_loss_status == SpotOrder.TradeStatus.POSITION
            and order.stop_loss_order_id
        ):
            try:
                sl_info = ex.fetch_order(id=order.stop_loss_order_id, symbol=symbol)
                if sl_info.get("remaining") == 0 and sl_info.get("status") == "closed":
                    avg = Decimal(str(sl_info.get("average") or sl_info.get("price") or 0))
                    sl_qty = remaining_qty if remaining_qty > 0 else total_qty

                    still_open = [
                        c for c in children if c.status == SpotTakeProfit.TradeStatus.POSITION
                    ]
                    for c in still_open:
                        try:
                            ex.cancel_order(id=c.tp_order_id, symbol=symbol)
                        except Exception:
                            pass
                        c.status = SpotTakeProfit.TradeStatus.CANCELLED
                        c.save()

                    sl_leg_pnl = _leg_pnl(direction, entry, avg, sl_qty)
                    total_pnl = realized_tp_pnl + sl_leg_pnl

                    order.exit_price = avg
                    order.status = SpotOrder.TradeStatus.CLOSED
                    order.stop_loss_status = SpotOrder.TradeStatus.CLOSED
                    order.closed_at = timezone.now()
                    order.pnl = total_pnl
                    order.pnl_percentage = (
                        (total_pnl / entry_val) * Decimal("100") if entry_val else Decimal("0")
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

        # --- 4. Partial TP fill, position still open: record realized PnL ---
        # Unlike futures there's no protective stop to resize/move here — a
        # TP-based spot signal is placed without an SL in the first place.
        if newly_filled:
            order.pnl = realized_tp_pnl
            order.pnl_percentage = (
                (realized_tp_pnl / entry_val) * Decimal("100") if entry_val else Decimal("0")
            )
            order.save()
            return True

        # Fallback heuristic scan — legacy orders only. This scans for *any*
        # closed sell order on the symbol, which would false-positive on a
        # TP leg that already got reconciled (and correctly accounted for)
        # in an earlier cycle above, forcing a premature full close and
        # orphaning any other still-open TP orders. Only orders with no
        # tracked TP legs at all (predating multi-TP spot support) should
        # ever reach this path.
        if children:
            return False
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
