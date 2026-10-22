from django.db import models

from apps.accounts.models import User


class MarketRiskSnapshot(models.Model):
    """One accepted 122Moon (HyperLiquid) controller message, schema v5.

    Rows are append-only: the "current" snapshot is the latest by
    (primary_state_time_ms, event_time_ms), so an accepted update never
    mutates a row an order worker may be reading. `allocation_profiles`
    holds the validated/normalized profiles (percentages as decimal
    strings); `raw_payload` keeps the controller JSON for audit."""

    event_id = models.CharField(max_length=128, unique=True)
    schema_version = models.PositiveSmallIntegerField()
    release_version = models.CharField(max_length=64, blank=True, default="")
    controller_version = models.CharField(max_length=64, blank=True, default="")
    event_type = models.CharField(max_length=50, blank=True, default="")

    event_time_ms = models.BigIntegerField()
    primary_state_time_ms = models.BigIntegerField()
    stale_after_sec = models.PositiveIntegerField()
    received_at = models.DateTimeField(auto_now_add=True)

    regime_key = models.CharField(max_length=30, blank=True, default="")
    regime_value = models.SmallIntegerField(null=True, blank=True)
    data_health = models.JSONField(default=dict)

    allocation_profiles = models.JSONField(default=dict)
    # Sizing constants from the controller (decimal strings / ints).
    spot_trade_pct_of_pool = models.DecimalField(max_digits=6, decimal_places=2)
    futures_margin_pct_of_pool = models.DecimalField(max_digits=6, decimal_places=2)
    futures_leverage = models.PositiveSmallIntegerField()

    raw_payload = models.JSONField(default=dict)

    class Meta:
        db_table = "market_risk_snapshots"
        ordering = ["-primary_state_time_ms", "-event_time_ms"]
        indexes = [models.Index(fields=["-primary_state_time_ms", "-event_time_ms"])]

    def __str__(self):
        return f"MarketRiskSnapshot({self.event_id})"


class RiskDecisionLog(models.Model):
    """Audit trail of HyperLiquid risk-gate decisions (allowed or rejected)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="risk_decisions")
    event_id = models.CharField(max_length=128, blank=True, default="")
    trading_mode = models.CharField(max_length=20, blank=True, default="")
    market = models.CharField(max_length=10)
    symbol = models.CharField(max_length=30, blank=True, default="")
    direction = models.CharField(max_length=10, blank=True, default="")
    allowed = models.BooleanField()
    reason = models.CharField(max_length=255, blank=True, default="")
    regime_key = models.CharField(max_length=30, blank=True, default="")
    effective_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    budget_usdc = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "risk_decision_logs"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]
