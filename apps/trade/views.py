from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.contrib import messages

import json

from apps.trade.task import (
    create_order_of_user_controller,
    close_order_of_user_controller,
    handle_futures_signal_controller,
)

from apps.trade.models import FutureOrder, FutureTakeProfit
from apps.accounts.models import UserKey
from apps.trade.utils.common import (
    make_futures_exchange,
    make_spot_exchange,
    get_symbol_last_price,
)
from apps.trade.utils.close_order import quick_close_position
from apps.trade.utils.close_market_order_spot import quick_close_spot_position
from apps.trade.utils.refresh_positions import refresh_futures_order, refresh_spot_order
from apps.trade.models import SpotOrder


@csrf_exempt
def trading_view_webhook(request):
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "Only POST requests are allowed"}, status=405
        )
    try:
        payload = json.loads(request.body)
        symbol = payload.get("symbol")
        side = payload.get("side")
        market = payload.get("market", None)
        tp = payload.get("tp")
        tps = payload.get("tps")  # optional list of {price, percent}
        sl = payload.get("sl")

        if not symbol or side not in ("buy", "sell"):
            return JsonResponse(
                {"status": "error", "message": "Missing/invalid params"}, status=400
            )

        if market == "futures":
            # Single orchestrator handles open/close/new per user
            handle_futures_signal_controller.delay(side, symbol, sl, tp, tps)
        else:
            if side == "buy":
                create_order_of_user_controller.delay(side, symbol, market, sl, tp)
            else:
                close_order_of_user_controller.delay(side, symbol, market)
        return JsonResponse({"status": "success", "message": "Webhook received"})
    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON payload"}, status=400
        )


@login_required
def update_futures_tp_sl(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    order_id = request.POST.get("order_id")
    tp = request.POST.get("tp")
    sl = request.POST.get("sl")
    mode = request.POST.get("mode")  # 'tp' or 'sl'

    if not order_id:
        return HttpResponseBadRequest("Missing order_id")

    try:
        order = FutureOrder.objects.get(
            id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION
        )
    except FutureOrder.DoesNotExist:
        messages.error(request, "Order not found or not open")
        return redirect("accounts:history")

    try:
        user_key = UserKey.objects.get(user=request.user, is_active=True)
        ex = make_futures_exchange(
            api_key=user_key.api_key, api_secret=user_key.api_secret
        )
        symbol = order.symbol
        qty = order.order_quantity
        inv_side = (
            "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"
        )

        # Handle SL or TP independently
        if mode == "sl":
            if sl:
                # Validate SL relative to current price and direction
                current = float(get_symbol_last_price(ex, symbol) or 0)
                sl_val = float(sl)
                if (
                    order.direction == FutureOrder.TradeDirection.LONG
                    and sl_val >= current
                ):
                    messages.error(request, "SL must be below current price for long.")
                    return redirect("accounts:history")
                if (
                    order.direction == FutureOrder.TradeDirection.SHORT
                    and sl_val <= current
                ):
                    messages.error(request, "SL must be above current price for short.")
                    return redirect("accounts:history")
                # Replace existing SL
                if order.stop_loss_order_id:
                    try:
                        ex.cancel_order(id=order.stop_loss_order_id, symbol=symbol)
                    except Exception:
                        pass
                sl_price = float(ex.priceToPrecision(symbol, float(sl)))
                sl_o = ex.create_order(
                    symbol=symbol,
                    side=inv_side,
                    type="STOP_MARKET",
                    amount=qty,
                    params={"stopPrice": sl_price, "reduceOnly": True},
                )
                order.stop_loss_order_id = sl_o["id"]
                order.stop_loss_price = sl_price
                order.stop_loss_status = FutureOrder.TradeStatus.POSITION
            else:
                # Remove SL only
                if order.stop_loss_order_id:
                    try:
                        ex.cancel_order(id=order.stop_loss_order_id, symbol=symbol)
                    except Exception:
                        pass
                order.stop_loss_order_id = ""
                order.stop_loss_price = 0
                order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED
        elif mode == "tp":
            if tp:
                # Validate TP relative to current price and direction
                current = float(get_symbol_last_price(ex, symbol) or 0)
                tp_val = float(tp)
                if (
                    order.direction == FutureOrder.TradeDirection.LONG
                    and tp_val <= current
                ):
                    messages.error(request, "TP must be above current price for long.")
                    return redirect("accounts:history")
                if (
                    order.direction == FutureOrder.TradeDirection.SHORT
                    and tp_val >= current
                ):
                    messages.error(request, "TP must be below current price for short.")
                    return redirect("accounts:history")
                # Replace with a single child TP at 100%
                try:
                    for child in list(order.tps.all()):
                        if child.status != FutureTakeProfit.TradeStatus.CLOSED:
                            if child.tp_order_id:
                                try:
                                    ex.cancel_order(id=child.tp_order_id, symbol=symbol)
                                except Exception:
                                    pass
                            child.delete()
                except Exception:
                    pass
                tp_price = float(ex.priceToPrecision(symbol, float(tp)))
                tp_o = ex.create_order(
                    symbol=symbol,
                    side=inv_side,
                    type="TAKE_PROFIT_MARKET",
                    amount=qty,
                    params={"stopPrice": tp_price, "reduceOnly": True},
                )
                FutureTakeProfit.objects.create(
                    order=order,
                    tp_order_id=tp_o.get("id", ""),
                    price=tp_price,
                    percent=100.0,
                    quantity=qty,
                    status=FutureTakeProfit.TradeStatus.POSITION,
                )
            else:
                # Remove TP only
                # Cancel and clear children
                try:
                    for child in list(order.tps.all()):
                        if child.status != FutureTakeProfit.TradeStatus.CLOSED:
                            if child.tp_order_id:
                                try:
                                    ex.cancel_order(id=child.tp_order_id, symbol=symbol)
                                except Exception:
                                    pass
                            child.delete()
                except Exception:
                    pass

        order.save()
        messages.success(request, "TP/SL updated")
    except Exception as e:
        messages.error(request, f"Failed to update TP/SL: {e}")

    return redirect("accounts:history")


@login_required
def update_spot_sl(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")

    order_id = request.POST.get("order_id")
    sl = request.POST.get("sl")

    if not order_id:
        return HttpResponseBadRequest("Missing order_id")

    try:
        order = SpotOrder.objects.get(
            id=order_id, user=request.user, status=SpotOrder.TradeStatus.POSITION
        )
    except SpotOrder.DoesNotExist:
        messages.error(request, "Order not found or not open")
        return redirect("accounts:history")

    try:
        user_key = UserKey.objects.get(user=request.user, is_active=True)
        ex = make_spot_exchange(api_key=user_key.api_key, api_secret=user_key.api_secret)
        symbol = order.symbol

        inv_side = (
            "sell" if order.direction == SpotOrder.TradeDirection.LONG else "buy"
        )
        amount = float(order.final_quantity or order.order_quantity)

        if sl:
            current = float(get_symbol_last_price(ex, symbol) or 0)
            sl_val = float(sl)
            if (
                order.direction == SpotOrder.TradeDirection.LONG and sl_val >= current
            ):
                messages.error(request, "SL must be below current price for long.")
                return redirect("accounts:history")
            if (
                order.direction == SpotOrder.TradeDirection.SHORT and sl_val <= current
            ):
                messages.error(request, "SL must be above current price for short.")
                return redirect("accounts:history")

            # Cancel existing SL if present
            if order.stop_loss_order_id:
                try:
                    ex.cancel_order(id=order.stop_loss_order_id, symbol=symbol)
                except Exception:
                    pass

            stop_p = float(ex.priceToPrecision(symbol, sl_val))
            amt_p = float(ex.amountToPrecision(symbol, amount))
            # Binance spot commonly expects STOP_LOSS_LIMIT for stop protection
            sl_o = ex.create_order(
                symbol=symbol,
                side=inv_side,
                type="STOP_LOSS_LIMIT",
                amount=amt_p,
                price=stop_p,
                params={"stopPrice": stop_p},
            )
            order.stop_loss_order_id = sl_o.get("id", "")
            order.stop_loss_price = stop_p
            order.stop_loss_status = SpotOrder.TradeStatus.POSITION
        else:
            # Remove SL
            if order.stop_loss_order_id:
                try:
                    ex.cancel_order(id=order.stop_loss_order_id, symbol=symbol)
                except Exception:
                    pass
            order.stop_loss_order_id = ""
            order.stop_loss_price = 0
            order.stop_loss_status = SpotOrder.TradeStatus.CANCELLED

        order.save()
        messages.success(request, "Spot SL updated")
    except Exception as e:
        messages.error(request, f"Failed to update Spot SL: {e}")

    return redirect("accounts:history")


@login_required
def update_futures_multi_tp(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")
    order_id = request.POST.get("order_id")
    prices = request.POST.getlist("tp_prices[]")
    percents = request.POST.getlist("tp_percents[]")
    if not order_id:
        return HttpResponseBadRequest("Missing order_id")
    if not prices or not percents:
        messages.info(request, "No editable TPs to update")
        return redirect("accounts:history")
    if len(prices) != len(percents):
        return HttpResponseBadRequest("Provide matching TP prices and percents")
    try:
        order = FutureOrder.objects.get(
            id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION
        )
    except FutureOrder.DoesNotExist:
        messages.error(request, "Order not found or not open")
        return redirect("accounts:history")

    # Build definitions
    try:
        defs = []
        total_pct = 0.0
        for p, pct in zip(prices, percents):
            if p == "" and pct == "":
                continue
            price = float(p)
            percent = float(pct)
            if percent <= 0:
                continue
            total_pct += percent
            defs.append({"price": price, "percent": percent})
        if total_pct <= 0 or total_pct > 100.0:
            messages.error(request, "Total TP percent must be between 0 and 100")
            return redirect("accounts:history")
    except Exception:
        messages.error(request, "Invalid TP inputs")
        return redirect("accounts:history")

    try:
        user_key = UserKey.objects.get(user=request.user, is_active=True)
        ex = make_futures_exchange(
            api_key=user_key.api_key, api_secret=user_key.api_secret
        )
        symbol = order.symbol
        inv_side = (
            "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"
        )
        cur = float(get_symbol_last_price(ex, symbol) or 0)

        # Cancel existing OPEN/POSITION TP(s); keep CLOSED ones
        try:
            for child in list(order.tps.all()):
                if child.status != FutureTakeProfit.TradeStatus.CLOSED:
                    if child.tp_order_id:
                        try:
                            ex.cancel_order(id=child.tp_order_id, symbol=symbol)
                        except Exception:
                            pass
                    child.delete()
        except Exception:
            pass

        base_qty = float(order.order_quantity)
        market_info = ex.market(symbol)
        min_amount = float(
            (market_info.get("limits") or {}).get("amount", {}).get("min") or 0
        )
        min_cost = float(
            (market_info.get("limits") or {}).get("cost", {}).get("min") or 0
        )
        # Create new multi-TPs
        placed = 0
        for tp_def in defs:
            price = float(tp_def["price"])
            percent = float(tp_def["percent"])
            if order.direction == FutureOrder.TradeDirection.LONG and price <= cur:
                messages.error(request, "Each TP must be above current for long")
                return redirect("accounts:history")
            if order.direction == FutureOrder.TradeDirection.SHORT and price >= cur:
                messages.error(request, "Each TP must be below current for short")
                return redirect("accounts:history")
            qty = base_qty * (percent / 100.0)
            qty_p = float(ex.amountToPrecision(symbol, qty))
            stop_p = float(ex.priceToPrecision(symbol, price))
            # Enforce minimum amount and notional if available
            if min_amount and qty_p < min_amount:
                continue
            if min_cost and (qty_p * stop_p) < min_cost:
                continue
            try:
                tp_o = ex.create_order(
                    symbol=symbol,
                    side=inv_side,
                    type="TAKE_PROFIT_MARKET",
                    amount=qty_p,
                    params={"stopPrice": stop_p, "reduceOnly": True},
                )
                FutureTakeProfit.objects.create(
                    order=order,
                    tp_order_id=tp_o.get("id", ""),
                    price=stop_p,
                    percent=percent,
                    quantity=qty_p,
                    status=FutureTakeProfit.TradeStatus.POSITION,
                )
                placed += 1
            except Exception as e:
                # skip failing leg and continue placing others
                continue
        order.save()
        if placed == 0:
            messages.error(
                request,
                "No TP orders were placed (min size/notional filters may apply)",
            )
        else:
            messages.success(request, f"Placed {placed} TP order(s)")
    except Exception as e:
        messages.error(request, f"Failed to set TPs: {e}")

    return redirect("accounts:history")


@login_required
def close_futures_order(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")
    order_id = request.POST.get("order_id")
    if not order_id:
        return HttpResponseBadRequest("Missing order_id")
    try:
        order = FutureOrder.objects.get(
            id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION
        )
        quick_close_position(order=order, user=request.user)
        messages.success(request, "Position closed")
    except FutureOrder.DoesNotExist:
        messages.error(request, "Order not found or not open")
    except Exception as e:
        messages.error(request, f"Failed to close position: {e}")
    return redirect("accounts:history")


@login_required
def close_spot_order(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")
    order_id = request.POST.get("order_id")
    if not order_id:
        return HttpResponseBadRequest("Missing order_id")
    try:
        order = SpotOrder.objects.get(
            id=order_id, user=request.user, status=SpotOrder.TradeStatus.POSITION
        )
        ok = quick_close_spot_position(order=order, user=request.user)
        if ok:
            messages.success(request, "Position closed")
        else:
            messages.error(request, "Failed to close position")
    except SpotOrder.DoesNotExist:
        messages.error(request, "Order not found or not open")
    except Exception as e:
        messages.error(request, f"Failed to close position: {e}")
    return redirect("accounts:history")


@login_required
def refresh_order(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")
    order_id = request.POST.get("order_id")
    market = request.POST.get("market")  # 'Futures' or 'Spot'
    if not order_id or not market:
        return HttpResponseBadRequest("Missing parameters")
    try:
        if market == "Futures":
            order = FutureOrder.objects.get(
                id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION
            )
            updated = refresh_futures_order(order)
        else:
            order = SpotOrder.objects.get(
                id=order_id, user=request.user, status=SpotOrder.TradeStatus.POSITION
            )
            updated = refresh_spot_order(order)
        if updated:
            messages.success(request, "Order refreshed")
        else:
            messages.info(request, "No changes detected")
    except (FutureOrder.DoesNotExist, SpotOrder.DoesNotExist):
        messages.error(request, "Order not found or not open")
    except Exception as e:
        messages.error(request, f"Refresh failed: {e}")
    return redirect("accounts:history")


@login_required
def toggle_ignore_signal(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")
    order_id = request.POST.get("order_id")
    market = request.POST.get("market")  # 'Futures' or 'Spot'
    if not order_id or not market:
        return HttpResponseBadRequest("Missing parameters")
    try:
        if market == "Futures":
            order = FutureOrder.objects.get(id=order_id, user=request.user)
        else:
            order = SpotOrder.objects.get(id=order_id, user=request.user)
        current = getattr(order, "ignore_opposite_signal", False)
        setattr(order, "ignore_opposite_signal", not current)
        order.save(update_fields=["ignore_opposite_signal", "updated_at"]) if hasattr(order, "updated_at") else order.save()
        state = "enabled" if not current else "disabled"
        messages.success(request, f"Ignore opposite signals {state} for this order")
    except (FutureOrder.DoesNotExist, SpotOrder.DoesNotExist):
        messages.error(request, "Order not found")
    except Exception as e:
        messages.error(request, f"Toggle failed: {e}")
    return redirect("accounts:history")
