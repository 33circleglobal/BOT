from django.urls import path
from .views import (
    trading_view_webhook,
    update_futures_tp_sl,
    update_spot_sl,
    close_futures_order,
    close_spot_order,
    toggle_ignore_signal,
    toggle_breakeven_sl,
    refresh_order,
    update_futures_multi_tp,
    risk_settings,
)

app_name = "trading"

urlpatterns = [
    path("webhook/", trading_view_webhook, name="webhook"),
    path("futures/update-risk/", update_futures_tp_sl, name="update_futures_tp_sl"),
    path("spot/update-sl/", update_spot_sl, name="update_spot_sl"),
    path("futures/close/", close_futures_order, name="close_futures_order"),
    path("spot/close/", close_spot_order, name="close_spot_order"),
    path("toggle-ignore/", toggle_ignore_signal, name="toggle_ignore_signal"),
    path("toggle-breakeven/", toggle_breakeven_sl, name="toggle_breakeven_sl"),
    path("refresh/", refresh_order, name="refresh_order"),
    path("futures/tps/", update_futures_multi_tp, name="update_futures_multi_tp"),
    path("risk-settings/", risk_settings, name="risk_settings"),
]
