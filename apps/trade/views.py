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

from apps.trade.models import FutureOrder
from apps.accounts.models import UserKey
from apps.trade.utils.common import make_futures_exchange, get_symbol_last_price
from apps.trade.utils.close_order import quick_close_position
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
        sl = payload.get("sl")

        if not symbol or side not in ("buy", "sell"):
            return JsonResponse({"status": "error", "message": "Missing/invalid params"}, status=400)

        if market == "futures":
            # Single orchestrator handles open/close/new per user
            handle_futures_signal_controller.delay(side, symbol, sl, tp)
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
        order = FutureOrder.objects.get(id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION)
    except FutureOrder.DoesNotExist:
        messages.error(request, "Order not found or not open")
        return redirect("accounts:history")

    try:
        user_key = UserKey.objects.get(user=request.user, is_active=True)
        ex = make_futures_exchange(api_key=user_key.api_key, api_secret=user_key.api_secret)
        symbol = order.symbol
        qty = order.order_quantity
        inv_side = "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"

        # Handle SL or TP independently
        if mode == "sl":
            if sl:
                # Validate SL relative to current price and direction
                current = float(get_symbol_last_price(ex, symbol) or 0)
                sl_val = float(sl)
                if order.direction == FutureOrder.TradeDirection.LONG and sl_val >= current:
                    messages.error(request, "SL must be below current price for long.")
                    return redirect("accounts:history")
                if order.direction == FutureOrder.TradeDirection.SHORT and sl_val <= current:
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
                if order.direction == FutureOrder.TradeDirection.LONG and tp_val <= current:
                    messages.error(request, "TP must be above current price for long.")
                    return redirect("accounts:history")
                if order.direction == FutureOrder.TradeDirection.SHORT and tp_val >= current:
                    messages.error(request, "TP must be below current price for short.")
                    return redirect("accounts:history")
                # Replace existing TP
                if order.tp_order_id:
                    try:
                        ex.cancel_order(id=order.tp_order_id, symbol=symbol)
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
                order.tp_order_id = tp_o["id"]
                order.tp_price = tp_price
                order.tp_status = FutureOrder.TradeStatus.POSITION
            else:
                # Remove TP only
                if order.tp_order_id:
                    try:
                        ex.cancel_order(id=order.tp_order_id, symbol=symbol)
                    except Exception:
                        pass
                order.tp_order_id = ""
                order.tp_price = 0
                order.tp_status = FutureOrder.TradeStatus.CANCELLED

        order.save()
        messages.success(request, "TP/SL updated")
    except Exception as e:
        messages.error(request, f"Failed to update TP/SL: {e}")

    return redirect("accounts:history")


@login_required
def close_futures_order(request):
    if request.method != "POST":
        return HttpResponseBadRequest("Invalid method")
    order_id = request.POST.get("order_id")
    if not order_id:
        return HttpResponseBadRequest("Missing order_id")
    try:
        order = FutureOrder.objects.get(id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION)
        quick_close_position(order=order, user=request.user)
        messages.success(request, "Position closed")
    except FutureOrder.DoesNotExist:
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
            order = FutureOrder.objects.get(id=order_id, user=request.user, status=FutureOrder.TradeStatus.POSITION)
            updated = refresh_futures_order(order)
        else:
            order = SpotOrder.objects.get(id=order_id, user=request.user, status=SpotOrder.TradeStatus.POSITION)
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
