from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

import json

from apps.trade.task import (
    create_order_of_user_controller,
    close_order_of_user_controller,
)


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
        create_order_of_user_controller.delay(side, symbol)
        close_order_of_user_controller(side, symbol)
        return JsonResponse({"status": "success", "message": "Webhook received"})
    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid JSON payload"}, status=400
        )
