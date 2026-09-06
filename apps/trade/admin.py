from django.contrib import admin

from .models import SpotOrder, FutureOrder, FutureTakeProfit, TradeSettings

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
