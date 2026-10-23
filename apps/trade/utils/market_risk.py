"""122Moon V3.2 (HyperLiquid) market-risk controller: ingest, freshness,
per-member profile selection, entry gate and pool-based sizing.

The controller webhook is a *portfolio-permission* message, never an order.
Binance flows do not touch this module (see risk_guard for the exchange
split). Percentages are handled as Decimal throughout.

Source of truth is the "122Moon V3.2 Developer Mechanics & Integration
Guide" (schema_version 5). The Pine script itself and a captured production
alert were NOT available when this was written; fields beyond those
documented in the guide are tolerated but ignored.
"""

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.db import connection, transaction

from apps.accounts.models import UserHyperLiquidKey
from apps.trade.models import (
    FutureOrder,
    MarketRiskSnapshot,
    RiskDecisionLog,
    SpotOrder,
    TradeSettings,
)

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 5
MODES = ("spot_only", "futures_only", "both")
_HUNDRED = Decimal(100)
_PCT_TOLERANCE = Decimal("0.01")
_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
_INGEST_LOCK_NAMESPACE = 911002

# "ALT feed missing" fallback from the guide (section 4): the most a profile
# may allocate when the primary regime feed is down.
ALT_MISSING_MAX_PCT = {"spot": Decimal(0), "futures": Decimal(20)}

DEFAULT_SPOT_TRADE_PCT = Decimal(15)
DEFAULT_FUTURES_MARGIN_PCT = Decimal(10)
DEFAULT_LEVERAGE = 3


class PayloadRejected(Exception):
    """Webhook payload failed validation / ordering checks."""


class DuplicateEvent(PayloadRejected):
    """event_id already accepted: idempotent no-op."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dec(value, field_name, lo=None, hi=None) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PayloadRejected(f"{field_name} must be a number")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PayloadRejected(f"{field_name} must be a number")
    if not d.is_finite():
        raise PayloadRejected(f"{field_name} must be finite")
    if lo is not None and d < lo:
        raise PayloadRejected(f"{field_name} must be >= {lo}")
    if hi is not None and d > hi:
        raise PayloadRejected(f"{field_name} must be <= {hi}")
    return d


def _int(value, field_name, lo=None, hi=None) -> int:
    if isinstance(value, bool) or value is None:
        raise PayloadRejected(f"{field_name} must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise PayloadRejected(f"{field_name} must be an integer")
        value = int(value)
    if not isinstance(value, int):
        try:
            value = int(str(value))
        except ValueError:
            raise PayloadRejected(f"{field_name} must be an integer")
    if lo is not None and value < lo:
        raise PayloadRejected(f"{field_name} must be >= {lo}")
    if hi is not None and value > hi:
        raise PayloadRejected(f"{field_name} must be <= {hi}")
    return value


def is_market_risk_v5_payload(payload) -> bool:
    """True only for the V3.2 HyperLiquid controller message. Routing is by
    exchange: any other exchange (e.g. the Binance controller, which has its
    own schema_version numbering) goes to the legacy handler untouched. A
    payload naming no exchange is treated as V3.2 only if it says schema 5."""
    if not isinstance(payload, dict):
        return False
    exchange = str(payload.get("exchange") or "").strip().lower()
    if exchange:
        return exchange == "hyperliquid"
    return payload.get("schema_version") == SUPPORTED_SCHEMA_VERSION


def _validate_profile(mode: str, raw, alt_ok: bool) -> dict:
    if not isinstance(raw, dict):
        raise PayloadRejected(f"allocation_profiles.{mode} missing or not an object")
    p = f"allocation_profiles.{mode}"
    spot = _dec(raw.get("spot_pct"), f"{p}.spot_pct", 0, 100)
    fut = _dec(raw.get("futures_pct"), f"{p}.futures_pct", 0, 100)
    reserve = _dec(raw.get("reserve_pct"), f"{p}.reserve_pct", 0, 100)
    max_spot = _int(raw.get("max_spot"), f"{p}.max_spot", 0, 1000)
    max_long = _int(raw.get("max_long"), f"{p}.max_long", 0, 1000)
    max_short = _int(raw.get("max_short"), f"{p}.max_short", 0, 1000)
    max_total = _int(raw.get("max_total"), f"{p}.max_total", 0, 2000)

    if abs(spot + fut + reserve - _HUNDRED) > _PCT_TOLERANCE:
        raise PayloadRejected(f"{p}: spot_pct + futures_pct + reserve_pct must equal 100")
    if max_total != max_long + max_short:
        raise PayloadRejected(f"{p}: max_total must equal max_long + max_short")
    if mode == "spot_only" and (fut != 0 or max_total or max_long or max_short):
        raise PayloadRejected(f"{p}: futures fields must be zero")
    if mode == "futures_only" and (spot != 0 or max_spot):
        raise PayloadRejected(f"{p}: spot fields must be zero")
    if not alt_ok and (
        spot > ALT_MISSING_MAX_PCT["spot"] or fut > ALT_MISSING_MAX_PCT["futures"]
    ):
        raise PayloadRejected(
            f"{p}: allocation exceeds the ALT-feed-missing fallback while data_health.alt_ok is false"
        )

    out = {
        "spot_pct": str(spot),
        "futures_pct": str(fut),
        "reserve_pct": str(reserve),
        "max_spot": max_spot,
        "max_long": max_long,
        "max_short": max_short,
        "max_total": max_total,
    }
    # Targets are informational only (not-yet-ramped goals); keep for display.
    for key in ("target_spot_pct", "target_futures_pct"):
        if raw.get(key) is not None:
            out[key] = str(_dec(raw[key], f"{p}.{key}", 0, 100))
    return out


def validate_payload(payload, now_ms: Optional[int] = None) -> dict:
    """Validates a schema-v5 payload. Returns the normalized fields used to
    build a MarketRiskSnapshot; raises PayloadRejected otherwise."""
    now_ms = _now_ms() if now_ms is None else now_ms
    if not isinstance(payload, dict):
        raise PayloadRejected("Payload must be a JSON object")
    if payload.get("action") != "update_market_risk":
        raise PayloadRejected("action must be update_market_risk")
    if _int(payload.get("schema_version"), "schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise PayloadRejected(f"Unsupported schema_version (expected {SUPPORTED_SCHEMA_VERSION})")
    if str(payload.get("exchange") or "").strip().lower() != "hyperliquid":
        raise PayloadRejected("exchange must be HyperLiquid")
    if str(payload.get("account_mode") or "").strip().lower() != "unified":
        raise PayloadRejected("account_mode must be unified")
    if str(payload.get("collateral_asset") or "").strip().upper() != "USDC":
        raise PayloadRejected("collateral_asset must be USDC")

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
        raise PayloadRejected("event_id must be a non-empty string (<=128 chars)")

    event_time_ms = _int(payload.get("event_time_ms"), "event_time_ms", 1)
    primary_ms = _int(payload.get("primary_state_time_ms"), "primary_state_time_ms", 1)
    stale_after = _int(
        payload.get("stale_after_sec"), "stale_after_sec", 60, settings.HL_MAX_STALE_AFTER_SEC
    )
    if event_time_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        raise PayloadRejected("event_time_ms is in the future")
    if primary_ms > event_time_ms:
        raise PayloadRejected("primary_state_time_ms is after event_time_ms")
    if event_time_ms + stale_after * 1000 < now_ms:
        raise PayloadRejected("Event is already stale")

    if payload.get("member_mode_selection") != "backend_per_member":
        raise PayloadRejected("member_mode_selection must be backend_per_member")
    supported = payload.get("supported_member_modes")
    if not isinstance(supported, list) or not set(MODES) <= set(supported):
        raise PayloadRejected("supported_member_modes must include all three modes")

    health = payload.get("data_health")
    if not isinstance(health, dict) or not all(
        isinstance(health.get(k), bool) for k in ("alt_ok", "btc_ok")
    ):
        raise PayloadRejected("data_health.alt_ok and data_health.btc_ok are required booleans")
    alt_ok = health["alt_ok"]

    regime = payload.get("current_market_regime")
    if not isinstance(regime, dict):
        raise PayloadRejected("current_market_regime missing")
    regime_value = _int(regime.get("value"), "current_market_regime.value", -2, 2)
    regime_key = str(regime.get("key") or "")[:30]

    raw_profiles = payload.get("allocation_profiles")
    if not isinstance(raw_profiles, dict):
        raise PayloadRejected("allocation_profiles missing")
    profiles = {m: _validate_profile(m, raw_profiles.get(m), alt_ok) for m in MODES}

    # Sizing constants. Only the pool-sizing percentages / leverage are read
    # from the legacy-named blocks; their *allocation* fields are ignored.
    spot_blk = payload.get("spot") if isinstance(payload.get("spot"), dict) else {}
    fut_blk = payload.get("futures") if isinstance(payload.get("futures"), dict) else {}
    spot_trade_pct = (
        _dec(spot_blk["trade_size_pct_of_spot_pool"], "spot.trade_size_pct_of_spot_pool", Decimal("0.01"), 100)
        if spot_blk.get("trade_size_pct_of_spot_pool") is not None
        else DEFAULT_SPOT_TRADE_PCT
    )
    fut_margin_pct = (
        _dec(fut_blk["margin_per_trade_pct_of_futures_pool"], "futures.margin_per_trade_pct_of_futures_pool", Decimal("0.01"), 100)
        if fut_blk.get("margin_per_trade_pct_of_futures_pool") is not None
        else DEFAULT_FUTURES_MARGIN_PCT
    )
    leverage = (
        _int(fut_blk["leverage"], "futures.leverage", 1, settings.HL_HARD_MAX_LEVERAGE)
        if fut_blk.get("leverage") is not None
        else DEFAULT_LEVERAGE
    )
    if fut_blk.get("margin_mode") not in (None, "isolated"):
        raise PayloadRejected("futures.margin_mode must be isolated")

    return {
        "event_id": event_id.strip(),
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "release_version": str(payload.get("release_version") or "")[:64],
        "controller_version": str(payload.get("controller_version") or "")[:64],
        "event_type": str(payload.get("event_type") or "")[:50],
        "event_time_ms": event_time_ms,
        "primary_state_time_ms": primary_ms,
        "stale_after_sec": stale_after,
        "regime_key": regime_key,
        "regime_value": regime_value,
        "data_health": health,
        "allocation_profiles": profiles,
        "spot_trade_pct_of_pool": spot_trade_pct,
        "futures_margin_pct_of_pool": fut_margin_pct,
        "futures_leverage": leverage,
    }


def ingest_payload(payload, now_ms: Optional[int] = None) -> MarketRiskSnapshot:
    """Validates and atomically stores a controller message.

    Raises DuplicateEvent for an already-accepted event_id and PayloadRejected
    for malformed, stale or out-of-order messages; an older state can never
    replace a newer accepted one (snapshots are append-only and "current" is
    the max by state time)."""
    fields = validate_payload(payload, now_ms)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)", [_INGEST_LOCK_NAMESPACE, 1]
            )
        if MarketRiskSnapshot.objects.filter(event_id=fields["event_id"]).exists():
            raise DuplicateEvent(f"Duplicate event_id {fields['event_id']}")
        latest = MarketRiskSnapshot.objects.first()
        if latest is not None:
            if fields["primary_state_time_ms"] < latest.primary_state_time_ms:
                raise PayloadRejected("Out-of-order: primary_state_time_ms older than accepted state")
            if (
                fields["primary_state_time_ms"] == latest.primary_state_time_ms
                and fields["event_time_ms"] <= latest.event_time_ms
            ):
                raise PayloadRejected("Out-of-order: event_time_ms not newer than accepted event")
        return MarketRiskSnapshot.objects.create(raw_payload=payload, **fields)


def get_current_snapshot() -> Optional[MarketRiskSnapshot]:
    return MarketRiskSnapshot.objects.first()


def snapshot_is_fresh(snapshot, now_ms: Optional[int] = None) -> bool:
    if snapshot is None:
        return False
    now_ms = _now_ms() if now_ms is None else now_ms
    return now_ms <= snapshot.event_time_ms + snapshot.stale_after_sec * 1000


# --------------------------------------------------------------------------
# Per-member gate
# --------------------------------------------------------------------------

@dataclass
class EntryDecision:
    allowed: bool
    reason: str = ""
    market: str = ""
    mode: str = ""
    snapshot: Optional[MarketRiskSnapshot] = None
    profile: dict = field(default_factory=dict)
    effective_pct: Optional[Decimal] = None
    reserve_pct: Decimal = Decimal(0)
    leverage: int = 1
    trade_pct: Decimal = Decimal(0)
    budget: Optional[Decimal] = None
    max_spot: int = 0
    max_long: int = 0
    max_short: int = 0
    max_total: int = 0


def _reject(reason, **kw) -> EntryDecision:
    return EntryDecision(allowed=False, reason=reason, **kw)


HL = "HYPERLIQUID"


def _hl_positions(model, user):
    return model.objects.filter(
        user=user,
        exchange=HL,
        status__in=[model.TradeStatus.POSITION, model.TradeStatus.OPEN],
    )


def _spot_committed(user) -> Decimal:
    total = Decimal(0)
    for o in _hl_positions(SpotOrder, user):
        qty = o.final_quantity or o.order_quantity or 0
        total += Decimal(qty) * Decimal(o.entry_price or 0)
    return total


def _futures_committed_margin(user) -> Decimal:
    total = Decimal(0)
    for o in _hl_positions(FutureOrder, user):
        lev = Decimal(max(o.leverage or 1, 1))
        total += Decimal(o.order_quantity or 0) * Decimal(o.entry_price or 0) / lev
    return total


def evaluate_entry(user, market: str, direction: Optional[str] = None, symbol: str = "") -> EntryDecision:
    """Slot-and-permission gate for a new risk-increasing HyperLiquid entry.
    `market` is "spot" or "futures"; `direction` ("LONG"/"SHORT") matters for
    futures. Fails closed on any missing/invalid/stale input. Does not look at
    capital (see size_entry)."""
    ts = TradeSettings.get_for_user(user)
    key = UserHyperLiquidKey.objects.filter(user=user).first()
    mode = key.trading_mode if key else ""
    base = dict(market=market, mode=mode)
    if key is None:
        return _reject("No HyperLiquid API key / trading mode configured", **base)

    if mode not in MODES:
        return _reject(f"Invalid trading mode {mode!r}", **base)
    if market == "spot" and mode == "futures_only":
        return _reject("Trading mode futures_only does not allow new spot entries", **base)
    if market == "futures" and mode == "spot_only":
        return _reject("Trading mode spot_only does not allow new futures entries", **base)

    snap = get_current_snapshot()
    if snap is None:
        return _reject("No market-risk snapshot accepted yet", **base)
    base["snapshot"] = snap
    if not snapshot_is_fresh(snap):
        return _reject(f"Market-risk snapshot {snap.event_id} is stale", **base)

    profile = (snap.allocation_profiles or {}).get(mode)
    if not isinstance(profile, dict):
        return _reject(f"Snapshot has no profile for mode {mode}", **base)
    base["profile"] = profile

    try:
        spot_pct = Decimal(profile["spot_pct"])
        fut_pct = Decimal(profile["futures_pct"])
        reserve = Decimal(profile["reserve_pct"])
        caps = {k: int(profile[k]) for k in ("max_spot", "max_long", "max_short", "max_total")}
    except (KeyError, InvalidOperation, ValueError, TypeError):
        return _reject("Snapshot profile is invalid", **base)

    eff = spot_pct if market == "spot" else fut_pct
    base.update(effective_pct=eff, reserve_pct=reserve)
    if eff <= 0:
        return _reject(f"Effective {market} allocation is 0%", **base)

    hard_spot = settings.HL_HARD_MAX_SPOT_POSITIONS
    hard_fut = settings.HL_HARD_MAX_FUTURES_POSITIONS
    if ts.hl_sync_from_webhook:
        max_spot = caps["max_spot"]
        max_long, max_short, max_total = caps["max_long"], caps["max_short"], caps["max_total"]
    else:
        # Auto-update off: member's manual caps apply (never above the
        # backend hard limits; the profile still gates market/allocation).
        max_spot = ts.hl_spot_max_positions
        max_long, max_short, max_total = (
            ts.hl_futures_max_long, ts.hl_futures_max_short, ts.hl_futures_max_positions
        )
    max_spot = min(max_spot, hard_spot)
    max_total = min(max_total, hard_fut)
    max_long = min(max_long, max_total)
    max_short = min(max_short, max_total)
    base.update(max_spot=max_spot, max_long=max_long, max_short=max_short, max_total=max_total)

    if market == "spot":
        if _hl_positions(SpotOrder, user).count() >= max_spot:
            return _reject(f"Max HyperLiquid spot positions reached ({max_spot})", **base)
        base["trade_pct"] = (
            snap.spot_trade_pct_of_pool if ts.hl_sync_from_webhook else ts.hl_spot_trade_pct
        )
    else:
        open_f = _hl_positions(FutureOrder, user)
        if open_f.count() >= max_total:
            return _reject(f"Max HyperLiquid futures positions reached ({max_total})", **base)
        if direction == FutureOrder.TradeDirection.LONG:
            if open_f.filter(direction=FutureOrder.TradeDirection.LONG).count() >= max_long:
                return _reject(f"Max HyperLiquid LONG futures positions reached ({max_long})", **base)
        else:
            if open_f.filter(direction=FutureOrder.TradeDirection.SHORT).count() >= max_short:
                return _reject(f"Max HyperLiquid SHORT futures positions reached ({max_short})", **base)
        base["trade_pct"] = (
            snap.futures_margin_pct_of_pool if ts.hl_sync_from_webhook else ts.hl_futures_margin_pct
        )
        base["leverage"] = max(
            1, min(snap.futures_leverage, ts.hl_futures_leverage, settings.HL_HARD_MAX_LEVERAGE)
        )

    return EntryDecision(allowed=True, **base)


def log_decision(user, decision: EntryDecision, symbol="", direction=""):
    try:
        RiskDecisionLog.objects.create(
            user=user,
            event_id=decision.snapshot.event_id if decision.snapshot else "",
            trading_mode=decision.mode,
            market=decision.market,
            symbol=symbol[:30],
            direction=(direction or "")[:10],
            allowed=decision.allowed,
            reason=decision.reason[:255],
            regime_key=decision.snapshot.regime_key if decision.snapshot else "",
            effective_pct=decision.effective_pct,
            budget_usdc=decision.budget,
        )
    except Exception:  # audit must never block or crash trading
        logger.exception("Failed to write RiskDecisionLog")


def size_entry(user, market, free_usdc, direction=None, symbol="") -> EntryDecision:
    """Full gate + pool sizing. Call after fetching the live free USDC
    collateral. On success `decision.budget` is the USDC amount to commit:
    spot notional, or futures *margin* (notional = budget * decision.leverage).

    eligible equity = free USDC + capital already committed in our tracked
    HyperLiquid positions/pending entries (an approximation of unified-account
    equity that avoids relying on exchange-specific "total" semantics).
    budget = min(pool x trade%, pool - committed_in_market, free USDC), where
    pool = equity x effective allocation%; the reserve is additionally
    protected by capping total committed capital at equity x (1 - reserve%)."""
    d = evaluate_entry(user, market, direction, symbol)
    if not d.allowed:
        log_decision(user, d, symbol, direction or "")
        return d

    free = Decimal(str(free_usdc or 0))
    spot_c = _spot_committed(user)
    fut_c = _futures_committed_margin(user)
    equity = free + spot_c + fut_c
    pool = equity * d.effective_pct / _HUNDRED
    committed = spot_c if market == "spot" else fut_c
    per_trade = pool * d.trade_pct / _HUNDRED
    pool_left = pool - committed
    reserve_left = equity * (_HUNDRED - d.reserve_pct) / _HUNDRED - (spot_c + fut_c)
    budget = min(per_trade, pool_left, reserve_left, free)

    if budget <= 0:
        d.allowed = False
        d.reason = (
            f"No uncommitted {market} pool capacity (equity={equity:.2f}, pool={pool:.2f}, "
            f"committed={committed:.2f}, free={free:.2f})"
        )
    else:
        d.budget = budget
    log_decision(user, d, symbol, direction or "")
    return d


def sync_settings_to_mode(user):
    """After a trading-mode change (API key settings): copies the latest
    accepted snapshot's profile for the new mode into the member's HyperLiquid
    settings (caps, per-trade sizes, leverage ceiling) so the Risk Settings
    UI shows the values that now apply. Returns the snapshot used, or None
    when nothing has been accepted yet (settings are left untouched)."""
    key = UserHyperLiquidKey.objects.filter(user=user).first()
    snap = get_current_snapshot()
    if key is None or snap is None:
        return None
    profile = (snap.allocation_profiles or {}).get(key.trading_mode)
    if not isinstance(profile, dict):
        return None
    ts = TradeSettings.get_for_user(user)
    ts.hl_spot_max_positions = min(profile["max_spot"], settings.HL_HARD_MAX_SPOT_POSITIONS)
    ts.hl_futures_max_positions = min(profile["max_total"], settings.HL_HARD_MAX_FUTURES_POSITIONS)
    ts.hl_futures_max_long = min(profile["max_long"], ts.hl_futures_max_positions)
    ts.hl_futures_max_short = min(profile["max_short"], ts.hl_futures_max_positions)
    ts.hl_spot_trade_pct = snap.spot_trade_pct_of_pool
    ts.hl_futures_margin_pct = snap.futures_margin_pct_of_pool
    ts.hl_futures_leverage = min(snap.futures_leverage, settings.HL_HARD_MAX_LEVERAGE)
    ts.save()
    return snap
