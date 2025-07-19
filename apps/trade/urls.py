from django.urls import path
from .views import trading_view_webhook

app_name = "trading"

urlpatterns = [
    path("webhook/", trading_view_webhook, name="webhook"),
]
