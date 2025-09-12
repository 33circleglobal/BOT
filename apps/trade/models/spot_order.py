from django.db import models
from apps.accounts.models import User


class SpotOrder(models.Model):
    class TradeDirection(models.TextChoices):
        LONG = "LONG", "Long"
        SHORT = "SHORT", "Short"

    class TradeStatus(models.TextChoices):
        OPEN = "OPEN", "Open"
        POSITION = "POSITION", "Position"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    class ExchangeType(models.TextChoices):
        BINANCE = "BINANCE", "Binance"
        BINANCE_FUTURES = "BINANCE_FUTURES", "Binance Futures"
        OTHER = "OTHER", "Other"

    # Core fields
    order_id = models.CharField(max_length=100, unique=True)
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=20, choices=TradeDirection.choices)

    # Quantity and pricing
    order_quantity = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    final_quantity = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    entry_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    exit_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    total_cost = models.DecimalField(max_digits=20, decimal_places=10, default=0)

    # Fee tracking
    entry_fee = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    entry_fee_currency = models.CharField(max_length=10, default="USDT")
    exit_fee = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    exit_fee_currency = models.CharField(max_length=10, default="USDT")
    total_fee = models.DecimalField(
        max_digits=10, decimal_places=6, null=True, blank=True
    )

    # PNL and status
    pnl = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    pnl_percentage = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(
        max_length=20, choices=TradeStatus.choices, default=TradeStatus.POSITION
    )

    # Trading context
    is_spot = models.BooleanField(default=False)
    exchange = models.CharField(
        max_length=20, choices=ExchangeType.choices, default=ExchangeType.BINANCE
    )
    leverage = models.IntegerField(default=1)  # 1 for spot

    # Protective stop (spot) — best-effort tracking
    stop_loss_order_id = models.CharField(max_length=100, default="", blank=True)
    stop_loss_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    stop_loss_status = models.CharField(
        max_length=20, choices=TradeStatus.choices, default=TradeStatus.POSITION
    )

    # User and timestamps
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "spot_orders"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["symbol", "created_at"]),
        ]

    def __str__(self):
        return f"{self.symbol} {self.direction} ({self.order_id})"

    # def save(self, *args, **kwargs):
    #     if self.exit_price and self.entry_price:
    #         price_diff = (self.exit_price) - self.entry_price
    #         self.pnl_percentage = (price_diff / self.entry_price) * 100
    #     super().save(*args, **kwargs)
