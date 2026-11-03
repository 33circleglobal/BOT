from django.db import models

from apps.accounts.models import User


class MarketRiskSnapshot(models.Model):
    """One accepted Throneex HyperLiquid V4.3.1 risk heartbeat (schema 9).

    Rows are append-only: the "current" snapshot is the latest by
    (primary_state_time_ms, event_time_ms), so an accepted update never
    mutates a row an order worker may be reading. `allocation_profiles`
    holds the validated/normalized profiles (percentages as decimal
    strings); `raw_payload` keeps the controller JSON for audit.

    Field mapping for schema 9: primary_state_time_ms = bar_close_time_ms
    (stable ordering key), event_time_ms = receipt time (freshness clock),
    regime_key/regime_value = canonical alt_market.regime,
    spot_trade_pct_of_pool = nominal Spot order % of *entry equity*,
    futures_margin_pct_of_pool = nominal Futures margin % of *entry equity*
    (the legacy column names are kept to avoid a destructive migration)."""

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


class HLEquityBaseline(models.Model):
    """Frozen policy baseline equity (policyBaselineEquityUSDC) per member.

    Converts the Both/Unified pool percentages into dollar caps. It is only
    refreshed at a regime-policy boundary (the canonical ALT regime changed
    since the baseline was taken) -- never on every heartbeat -- so equity
    swings and manual isolated-margin top-ups cannot silently resize pools."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="hl_equity_baseline")
    baseline_equity = models.DecimalField(max_digits=20, decimal_places=6)
    regime_key = models.CharField(max_length=30, blank=True, default="")
    refresh_reason = models.CharField(max_length=100, blank=True, default="")
    event_id = models.CharField(max_length=128, blank=True, default="")
    refreshed_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "hl_equity_baselines"


class HLOrderReservation(models.Model):
    """Local capacity reservation, created under the per-member lock before
    an order is sent to HyperLiquid.

    PENDING     reserved, nothing sent yet (safe to release on local failure)
    SUBMITTING  the exchange call was attempted; outcome not yet known
    UNCERTAIN   submission failed ambiguously (timeout/crash); capacity stays
                held until reconciled -- never released on a guess
    CONFIRMED   the order/position row exists; that row now carries the
                commitment, so the reservation no longer counts (no double
                counting)
    RELEASED    definitively not placed; capacity freed
    """

    class Status(models.TextChoices):
        PENDING = "PENDING"
        SUBMITTING = "SUBMITTING"
        UNCERTAIN = "UNCERTAIN"
        CONFIRMED = "CONFIRMED"
        RELEASED = "RELEASED"

    ACTIVE = ("PENDING", "SUBMITTING", "UNCERTAIN")

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hl_reservations")
    market = models.CharField(max_length=10)  # "spot" | "futures"
    direction = models.CharField(max_length=10, blank=True, default="")
    symbol = models.CharField(max_length=30, blank=True, default="")
    # Spot: quote amount. Futures: initial margin.
    amount_usdc = models.DecimalField(max_digits=20, decimal_places=6)
    notional_usdc = models.DecimalField(max_digits=20, decimal_places=6)
    stop_risk_usdc = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    event_id = models.CharField(max_length=128, blank=True, default="")
    order_id = models.CharField(max_length=100, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "hl_order_reservations"
        indexes = [models.Index(fields=["user", "status"])]
