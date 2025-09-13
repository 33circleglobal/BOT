from django.urls import path
from .views import (
    trading_view_webhook,
    update_futures_tp_sl,
    close_futures_order,
    close_spot_order,
    toggle_ignore_signal,
    refresh_order,
    update_futures_multi_tp,
)

app_name = "trading"

urlpatterns = [
    path("webhook/", trading_view_webhook, name="webhook"),
    path("futures/update-risk/", update_futures_tp_sl, name="update_futures_tp_sl"),
    path("futures/close/", close_futures_order, name="close_futures_order"),
    path("spot/close/", close_spot_order, name="close_spot_order"),
    path("toggle-ignore/", toggle_ignore_signal, name="toggle_ignore_signal"),
    path("refresh/", refresh_order, name="refresh_order"),
    path("futures/tps/", update_futures_multi_tp, name="update_futures_multi_tp"),
]
