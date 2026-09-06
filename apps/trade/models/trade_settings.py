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

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "trade_settings"

    def __str__(self):
        return f"TradeSettings({self.user_id})"

    @classmethod
    def get_for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj
