from django.contrib import admin

from .models import (
    SpotOrder,
    FutureOrder,
    FutureTakeProfit,
    SpotTakeProfit,
    TradeSettings,
    WebhookLog,
)

# Register your models here.


@admin.register(SpotOrder)
class SpotOrderAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "order_id", "symbol", "direction", "status"]


@admin.register(FutureOrder)
class FutureOrderAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "order_id", "symbol", "direction", "status"]


@admin.register(FutureTakeProfit)
class FutureTakeProfitAdmin(admin.ModelAdmin):
    list_display = ["id", "order", "tp_order_id", "price", "percent", "status"]


@admin.register(SpotTakeProfit)
class SpotTakeProfitAdmin(admin.ModelAdmin):
    list_display = ["id", "order", "tp_order_id", "price", "percent", "status"]


@admin.register(TradeSettings)
class TradeSettingsAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "futures_max_positions",
        "futures_max_long",
        "futures_max_short",
        "spot_max_positions",
    ]


@admin.register(WebhookLog)
class WebhookLogAdmin(admin.ModelAdmin):
    list_display = ["id", "created_at", "status", "exchange", "market", "symbol", "side", "action"]
    list_filter = ["status", "exchange", "market"]
    search_fields = ["symbol", "raw_body"]
    readonly_fields = [
        "raw_body",
        "payload",
        "action",
        "symbol",
        "side",
        "market",
        "exchange",
        "status",
        "error_message",
        "remote_addr",
        "created_at",
    ]
