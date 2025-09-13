from django.db import models
from apps.accounts.models import User


class FutureOrder(models.Model):

    class TradeDirection(models.TextChoices):
        LONG = "LONG", "Long"
        SHORT = "SHORT", "Short"

    class TradeStatus(models.TextChoices):
        OPEN = "OPEN", "Open"
        POSITION = "POSITION", "Position"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    order_id = models.CharField(max_length=100, unique=True)
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=20, choices=TradeDirection.choices)
    status = models.CharField(
        max_length=20, choices=TradeStatus.choices, default=TradeStatus.POSITION
    )
    leverage = models.IntegerField(default=1)
    order_quantity = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    entry_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    entry_fee = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    entry_fee_currency = models.CharField(max_length=10, default="USDT")
    # stop loss
    stop_loss_order_id = models.CharField(max_length=100, unique=True)
    stop_loss_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    stop_loss_fee = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    stop_loss_status = models.CharField(
        max_length=20, choices=TradeStatus.choices, default=TradeStatus.POSITION
    )
    # total fee
    total_fee = models.DecimalField(
        max_digits=10, decimal_places=6, null=True, blank=True
    )

    # PNL
    pnl = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    pnl_percentage = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # User and timestamps
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "future_orders"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["symbol", "created_at"]),
        ]
