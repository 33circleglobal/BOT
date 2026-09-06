from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from apps.accounts.models import User


class TradeSettings(models.Model):
    """Per-user, UI-configurable risk limits (see apps.trade.utils.risk_guard)."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="trade_settings"
    )

    futures_max_positions = models.PositiveIntegerField(default=7)
    futures_max_long = models.PositiveIntegerField(default=4)
    futures_max_short = models.PositiveIntegerField(default=4)
    spot_max_positions = models.PositiveIntegerField(default=7)

    # Position sizing: % of available balance committed to a single new trade.
    futures_position_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=10,
        validators=[MinValueValidator(0.01), MaxValueValidator(100)],
    )
    spot_position_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=100,
        validators=[MinValueValidator(0.01), MaxValueValidator(100)],
    )

    # Leverage applied to new futures positions.
    futures_leverage = models.PositiveIntegerField(
        default=5, validators=[MinValueValidator(1), MaxValueValidator(125)]
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "trade_settings"

    def __str__(self):
        return f"TradeSettings({self.user_id})"

    @classmethod
    def get_for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj
