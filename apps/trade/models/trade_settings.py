from datetime import timedelta

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

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

    # When on, the "update_futures_risk"/"update_spot_risk" webhook actions
    # (see apps.trade.views.trading_view_webhook) overwrite this user's
    # max-position fields above based on the current market regime. When
    # off, this user's settings are left untouched by those webhook updates.
    sync_risk_from_webhook = models.BooleanField(default=True)

    # Staleness guard: a TradingView-side outage (or a dropped/failed HTTP
    # call) can silently stop the "update_market_risk" webhook from firing
    # while the actual market regime keeps moving — e.g. our limits stayed
    # tuned for a bull regime while the market had already flipped bearish,
    # and new trades kept opening against the stale settings until a human
    # noticed. When `sync_risk_from_webhook` is on, we now refuse to open
    # any *new* position once too long has passed since the last update
    # (see `is_market_data_stale`); existing positions are left alone.
    last_market_risk_update = models.DateTimeField(null=True, blank=True)
    max_market_data_age_minutes = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(1440)],
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

    def is_market_data_stale(self) -> bool:
        """True when new positions should be blocked because the
        market-regime webhook hasn't been heard from recently enough.

        Only applies when this user relies on the webhook
        (`sync_risk_from_webhook`); a user who manages limits manually never
        expects webhook updates, so there's nothing to go stale. A user who
        does rely on it but has never received a single update yet is
        treated as stale — we'd otherwise be trading on a still-default
        regime assumption rather than a confirmed one.
        """
        if not self.sync_risk_from_webhook:
            return False
        if not self.last_market_risk_update:
            return True
        age = timezone.now() - self.last_market_risk_update
        return age > timedelta(minutes=self.max_market_data_age_minutes)
