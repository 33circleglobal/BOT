from django.db import models
from .future_order import FutureOrder


class FutureTakeProfit(models.Model):
    class TradeStatus(models.TextChoices):
        OPEN = "OPEN", "Open"
        POSITION = "POSITION", "Position"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    order = models.ForeignKey(FutureOrder, on_delete=models.CASCADE, related_name="tps")
    tp_order_id = models.CharField(max_length=100, blank=True, default="")
    price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    percent = models.DecimalField(max_digits=7, decimal_places=3, default=0)  # of base qty
    quantity = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    status = models.CharField(max_length=20, choices=TradeStatus.choices, default=TradeStatus.POSITION)
    fee = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "future_take_profits"
        indexes = [
            models.Index(fields=["order", "status"]),
        ]

