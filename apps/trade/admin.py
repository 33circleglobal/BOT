from django import forms
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


class SpotOrderAdminForm(forms.ModelForm):
    """order_id is only unique against SpotOrder.order_id itself, so a
    manually-reconstructed row referencing an execution that's already
    represented as some *other* order's DCA leg or TP fill (its own Binance
    order id, distinct from that order's primary order_id) sails through
    unnoticed — this is exactly how order 63 duplicated order 56's DCA fill.
    Catch that here by also checking dca_order_id/tp_order_id across every
    other SpotOrder."""

    class Meta:
        model = SpotOrder
        fields = "__all__"

    def clean_order_id(self):
        order_id = self.cleaned_data["order_id"]
        others = SpotOrder.objects.exclude(pk=self.instance.pk)
        dca_hit = others.filter(dca_order_id=order_id).first()
        if dca_hit:
            raise forms.ValidationError(
                f"This Binance order id is already recorded as the DCA fill "
                f"of SpotOrder #{dca_hit.id} ({dca_hit.symbol}) — creating a "
                f"new order for it would double-count that position."
            )
        tp_hit = SpotTakeProfit.objects.filter(tp_order_id=order_id).select_related("order").first()
        if tp_hit:
            raise forms.ValidationError(
                f"This Binance order id is already recorded as a take-profit "
                f"fill of SpotOrder #{tp_hit.order_id} ({tp_hit.order.symbol}) — "
                f"creating a new order for it would double-count that position."
            )
        return order_id


@admin.register(SpotOrder)
class SpotOrderAdmin(admin.ModelAdmin):
    form = SpotOrderAdminForm
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
