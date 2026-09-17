from apps.accounts.models import UserHyperLiquidKey
from apps.trade.models import FutureOrder, SpotOrder, FutureTakeProfit, SpotTakeProfit
from apps.trade.utils.common import compute_default_sl, opposite_side
from apps.trade.utils.hyperliquid_common import (
    make_hyperliquid_exchange,
    to_hyperliquid_symbol,
    create_trigger_order,
    fetch_hl_order_status,
    fetch_hl_fill_data,
    cancel_hl_order,
)

import logging
from django.utils import timezone
from decimal import Decimal
from uuid import uuid4

logger = logging.getLogger(__name__)

DUST = Decimal("0.00000001")


def _leg_pnl(direction, entry: Decimal, exit_price: Decimal, qty: Decimal) -> Decimal:
    if direction == FutureOrder.TradeDirection.LONG:
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def refresh_hyperliquid_futures_order(order: FutureOrder) -> bool:
    """HyperLiquid equivalent of refresh_futures_order — see that function
    for the full behavior description. The only mechanical difference is
    that HyperLiquid trigger (SL/TP) orders are regular orders, fetched and
    cancelled via fetch_hl_order_status/cancel_hl_order instead of Binance's
    dedicated algo-order endpoint.
    """
    if order.status != FutureOrder.TradeStatus.POSITION:
        return False
    try:
        hl_key = UserHyperLiquidKey.objects.get(user=order.user, is_active=True)
        ex = make_hyperliquid_exchange(
            wallet_address=hl_key.get_master_wallet_address(),
            private_key=hl_key.get_api_private_key(),
        )
        symbol = to_hyperliquid_symbol(order.symbol, "futures")
        entry = Decimal(str(order.entry_price))
        total_qty = Decimal(str(order.order_quantity or 0))
        direction = order.direction
        entry_side = "buy" if direction == FutureOrder.TradeDirection.LONG else "sell"
        sl_side = opposite_side(entry_side)
        leverage = Decimal(str(order.leverage or 1))
        notional = entry * total_qty
        margin = (notional / leverage) if leverage else notional
        fill_data = fetch_hl_fill_data(ex, symbol)

        # --- 1. Reconcile any newly-filled TP legs ---------------------------
        children = list(FutureTakeProfit.objects.filter(order=order))
        newly_filled = False
        for child in children:
            if child.status != FutureTakeProfit.TradeStatus.POSITION:
                continue
            tp_info = fetch_hl_order_status(ex, symbol, child.tp_order_id, fill_data)
            if tp_info and tp_info["filled"]:
                child.price = tp_info["average"]
                if tp_info["closed_pnl"] is not None:
                    # HyperLiquid's own realized PnL for this fill — the
                    # authoritative number (matches their fill history and
                    # trackers like HyperDash), not a recomputation from
                    # price/qty that can't see fees or trigger-order
                    # slippage quirks.
                    child.pnl = Decimal(str(tp_info["closed_pnl"]))
                    child.fee = Decimal(str(tp_info["fee"]))
                    order.total_fee = float(order.total_fee or 0) + tp_info["fee"]
                else:
                    child.pnl = _leg_pnl(
                        direction, entry, Decimal(str(tp_info["average"])), Decimal(str(child.quantity))
                    )
                child.status = FutureTakeProfit.TradeStatus.CLOSED
                child.save()
                newly_filled = True

        closed_children = [
            c for c in children if c.status == FutureTakeProfit.TradeStatus.CLOSED
        ]
        realized_tp_qty = sum((Decimal(str(c.quantity)) for c in closed_children), Decimal("0"))
        realized_tp_pnl = sum((Decimal(str(c.pnl)) for c in closed_children), Decimal("0"))
        remaining_qty = total_qty - realized_tp_qty
        if remaining_qty < 0:
            remaining_qty = Decimal("0")

        # --- 2. All TPs filled: position fully closed without the SL --------
        if closed_children and remaining_qty <= DUST:
            if (
                order.stop_loss_status == FutureOrder.TradeStatus.POSITION
                and order.stop_loss_order_id
            ):
                cancel_hl_order(ex, symbol, order.stop_loss_order_id)
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
            sl_info = fetch_hl_order_status(ex, symbol, order.stop_loss_order_id, fill_data)
            if sl_info and sl_info["filled"]:
                exit_avg = Decimal(str(sl_info["average"]))
                sl_qty = remaining_qty if remaining_qty > 0 else total_qty
                if sl_info["closed_pnl"] is not None:
                    sl_leg_pnl = Decimal(str(sl_info["closed_pnl"]))
                    order.total_fee = float(order.total_fee or 0) + sl_info["fee"]
                else:
                    sl_leg_pnl = _leg_pnl(direction, entry, exit_avg, sl_qty)

                still_open = [
                    c for c in children if c.status == FutureTakeProfit.TradeStatus.POSITION
                ]
                for c in still_open:
                    cancel_hl_order(ex, symbol, c.tp_order_id)
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
        sl_needs_resize = newly_filled or order.stop_loss_status == FutureOrder.TradeStatus.FAILED
        has_live_sl = (
            order.stop_loss_status == FutureOrder.TradeStatus.POSITION
            and order.stop_loss_order_id
            and not str(order.stop_loss_order_id).startswith("DISABLED-")
        )
        if sl_needs_resize and remaining_qty > 0 and (has_live_sl or order.stop_loss_status == FutureOrder.TradeStatus.FAILED):
            breakeven = float(entry)
            already_at_breakeven = abs(float(order.stop_loss_price) - breakeven) < 1e-8
            if closed_children and order.move_sl_to_breakeven:
                trigger_price = breakeven
            else:
                trigger_price = float(order.stop_loss_price)
                if trigger_price <= 0:
                    trigger_price = compute_default_sl(float(entry), entry_side)
                    logger.warning(
                        f"[refresh] HyperLiquid order {order.id} had an invalid "
                        f"stop_loss_price ({order.stop_loss_price}); using computed "
                        f"default SL {trigger_price} instead."
                    )
            if has_live_sl:
                cancel_hl_order(ex, symbol, order.stop_loss_order_id)
            new_qty = float(ex.amountToPrecision(symbol, float(remaining_qty)))
            try:
                new_sl = create_trigger_order(
                    ex,
                    symbol,
                    sl_side,
                    new_qty,
                    trigger_price,
                    take_profit=False,
                )
                order.stop_loss_order_id = new_sl["id"]
                order.stop_loss_status = FutureOrder.TradeStatus.POSITION
                order.stop_loss_price = trigger_price
                if closed_children and order.move_sl_to_breakeven and not already_at_breakeven:
                    logger.info(
                        f"[sl] Moved SL to breakeven ({trigger_price}) for HyperLiquid "
                        f"order {order.id} after a TP fill"
                    )
            except Exception as e:
                order.stop_loss_status = FutureOrder.TradeStatus.FAILED
                order.stop_loss_order_id = f"RESIZE-FAILED-{uuid4()}"
                logger.error(
                    f"[refresh] Failed to resize SL for HyperLiquid order {order.id} to "
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
        logger.error(f"Failed to refresh HyperLiquid futures order {order.id}: {e}")
        return False


def refresh_hyperliquid_spot_order(order: SpotOrder) -> bool:
    """HyperLiquid equivalent of refresh_spot_order — see that function for
    the full behavior description (TP/SL/DCA reconciliation). HyperLiquid
    has no algo-order quirk, so both TP legs and the SL are queried via the
    same fetch_hl_order_status helper used for futures.
    """
    if order.status != SpotOrder.TradeStatus.POSITION:
        return False
    try:
        hl_key = UserHyperLiquidKey.objects.get(user=order.user, is_active=True)
        ex = make_hyperliquid_exchange(
            wallet_address=hl_key.get_master_wallet_address(),
            private_key=hl_key.get_api_private_key(),
        )
        symbol = to_hyperliquid_symbol(order.symbol, "spot")
        fill_data = fetch_hl_fill_data(ex, symbol)

        # --- 0. DCA (average-down) fill check ---------------------------------
        if order.dca_status == SpotOrder.TradeStatus.POSITION and order.dca_order_id:
            dca_info = fetch_hl_order_status(ex, symbol, order.dca_order_id, fill_data)
            if dca_info and dca_info["filled"]:
                old_qty = Decimal(str(order.order_quantity or 0))
                old_final = Decimal(str(order.final_quantity or order.order_quantity or 0))
                old_price = Decimal(str(order.entry_price))
                dca_qty = Decimal(str(order.dca_quantity or 0))
                dca_fill_price = Decimal(str(dca_info["average"] or order.dca_price))

                new_qty = old_qty + dca_qty
                new_avg = (
                    ((old_qty * old_price) + (dca_qty * dca_fill_price)) / new_qty
                    if new_qty
                    else old_price
                )
                new_final = old_final + dca_qty

                order.order_quantity = new_qty
                order.entry_price = new_avg
                order.final_quantity = new_final
                order.dca_status = SpotOrder.TradeStatus.CLOSED

                for child in SpotTakeProfit.objects.filter(
                    order=order, status=SpotTakeProfit.TradeStatus.POSITION
                ):
                    cancel_hl_order(ex, symbol, child.tp_order_id)
                    new_leg_qty = float(new_final) * (float(child.percent) / 100.0)
                    new_leg_qty_p = float(ex.amountToPrecision(symbol, new_leg_qty))
                    try:
                        tp_o = ex.create_order(
                            symbol=symbol,
                            side="sell",
                            type="limit",
                            amount=new_leg_qty_p,
                            price=float(child.price),
                        )
                        child.tp_order_id = str(tp_o.get("id", ""))
                        child.quantity = new_leg_qty_p
                        child.save()
                    except Exception as e:
                        logger.error(
                            f"[dca] Failed to resize HyperLiquid spot TP {child.id} after "
                            f"DCA fill for {symbol}: {e}",
                            exc_info=True,
                        )
                        child.status = SpotTakeProfit.TradeStatus.FAILED
                        child.save()

                order.save()
                logger.info(
                    f"[dca] HyperLiquid spot order {order.id} averaged down: "
                    f"qty {old_qty}->{new_qty}, entry {old_price}->{new_avg}"
                )
                return True

        entry = Decimal(str(order.entry_price))
        total_qty = Decimal(str(order.final_quantity or order.order_quantity))
        direction = order.direction

        # --- 1. Reconcile any newly-filled TP legs ---------------------------
        children = list(SpotTakeProfit.objects.filter(order=order))
        newly_filled = False
        for child in children:
            if child.status != SpotTakeProfit.TradeStatus.POSITION:
                continue
            info = fetch_hl_order_status(ex, symbol, child.tp_order_id, fill_data)
            if info is None:
                continue
            if info["filled"]:
                child.price = info["average"] or float(child.price)
                if info["fee"]:
                    child.fee = Decimal(str(info["fee"]))
                child.status = SpotTakeProfit.TradeStatus.CLOSED
                child.save()
                newly_filled = True
            elif info["raw"].get("status") in ("canceled", "cancelled", "expired", "rejected"):
                child.status = SpotTakeProfit.TradeStatus.CANCELLED
                child.save()

        if (
            newly_filled
            and order.dca_status == SpotOrder.TradeStatus.POSITION
            and order.dca_order_id
        ):
            cancel_hl_order(ex, symbol, order.dca_order_id)
            order.dca_status = SpotOrder.TradeStatus.CANCELLED

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
                cancel_hl_order(ex, symbol, order.stop_loss_order_id)
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
            sl_info = fetch_hl_order_status(ex, symbol, order.stop_loss_order_id, fill_data)
            if sl_info and sl_info["filled"]:
                avg = Decimal(str(sl_info["average"]))
                sl_qty = remaining_qty if remaining_qty > 0 else total_qty

                still_open = [
                    c for c in children if c.status == SpotTakeProfit.TradeStatus.POSITION
                ]
                for c in still_open:
                    cancel_hl_order(ex, symbol, c.tp_order_id)
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
                order.save()
                return True

        # --- 4. Partial TP fill, position still open: record realized PnL ---
        if newly_filled:
            order.pnl = realized_tp_pnl
            order.pnl_percentage = (
                (realized_tp_pnl / entry_val) * Decimal("100") if entry_val else Decimal("0")
            )
            order.save()
            return True

        return False
    except Exception as e:
        logger.error(f"Failed to refresh HyperLiquid spot order {order.id}: {e}")
        return False
