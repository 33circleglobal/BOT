from django.db import models

from apps.accounts.models import User


# Create your models here.
class Order(models.Model):
    class TradeDirection(models.TextChoices):
        LONG = "LONG", "Long"
        SHORT = "SHORT", "Short"

    class TradeStatus(models.TextChoices):
        OPEN = "OPEN", "Open"
        POSITION = "POSITION", "Position"
        CLOSED = "CLOSED", "Closed"

    order_id = models.CharField(max_length=100, unique=True)
    symbol = models.CharField(max_length=20)
    direction = models.CharField(max_length=20, choices=TradeDirection.choices)
    order_quantity = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    entry_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    exit_price = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    fee = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    pnl = models.DecimalField(max_digits=20, decimal_places=10, default=0)
    status = models.CharField(
        max_length=20, choices=TradeDirection.choices, default=TradeStatus.POSITION
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "orders"
