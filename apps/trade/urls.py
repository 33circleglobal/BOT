from django.urls import path
from .views import trading_view_webhook, update_futures_tp_sl, close_futures_order, refresh_order

app_name = "trading"

urlpatterns = [
    path("webhook/", trading_view_webhook, name="webhook"),
    path("futures/update-risk/", update_futures_tp_sl, name="update_futures_tp_sl"),
    path("futures/close/", close_futures_order, name="close_futures_order"),
    path("refresh/", refresh_order, name="refresh_order"),
]
