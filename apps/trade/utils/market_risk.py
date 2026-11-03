"""Throneex HyperLiquid Risk Controller V4.3.1 (schema 9): ingest, freshness,
per-member profile selection, entry gate, funding pools and reservations.

The webhook is a *portfolio-permission* heartbeat, never an order: it sets
market limits and (for Both/Unified) funding policy. The backend stays the
authority for member mode, real balances, positions, reservations and every
safeguard; the strictest applicable limit always wins. Binance flows never
touch this module (see risk_guard for the exchange split).

Source: the V4.3.1 integration brief. The handoff PDF, the Pine script and the
reference Backend_Risk_Guard.js were NOT readable/available when this was
written, so the parser implements the documented contract only. Unresolved
assumptions are marked "ASSUMPTION" below and listed in the delivery report.
Percentages and money are Decimal throughout.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import UserHyperLiquidKey
from apps.trade.models import (
    FutureOrder,
    HLEquityBaseline,
    HLOrderReservation,
    MarketRiskSnapshot,
    RiskDecisionLog,
    SpotOrder,
    TradeSettings,
)

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 9
MODES = ("spot_only", "futures_only", "both")
REGIMES = {"strong_bull": 2, "bull": 1, "neutral": 0, "bear": -1, "strong_bear": -2}
RISK_EVENT_TYPES = ("heartbeat", "market_state_change", "risk_limit_change")
HEARTBEAT_REASONS = ("startup", "primary_period", "deadman_refresh", "change")
_HUNDRED = Decimal(100)
_PCT_TOLERANCE = Decimal("0.01")
_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
_INGEST_LOCK_NAMESPACE = 911002

# Controller-published per-feed flags that mean "the chart timeframe is
# invalid". ASSUMPTION: exact key names are not in the brief; any of these in
# data_health blocks new entries.
_TF_OK_KEYS = ("timeframe_ok", "chart_timeframe_ok", "tf_ok")
_TF_BAD_KEYS = ("invalid_timeframe", "invalid_chart_timeframe", "timeframe_invalid")

DEFAULT_SPOT_ORDER_PCT = Decimal(15)
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


def is_market_risk_payload(payload) -> bool:
    """True when the message belongs to the HyperLiquid controller. Routing is
    by exchange: any other exchange (e.g. the Binance controller, which has its
    own schema_version numbering) goes to the legacy handler untouched. A
    payload naming no exchange is treated as HyperLiquid only if it says
    schema 9."""
    if not isinstance(payload, dict):
        return False
    exchange = str(payload.get("exchange") or "").strip().lower()
    if exchange:
        return exchange == "hyperliquid"
    return payload.get("schema_version") == SUPPORTED_SCHEMA_VERSION


# Backwards-compatible name used by the webhook view.
is_market_risk_v5_payload = is_market_risk_payload


# --------------------------------------------------------------------------
# Schema-9 validation
# --------------------------------------------------------------------------

def _caps(raw, p, *, need_spot=False, need_futures=False) -> dict:
    """Reads max_spot/max_long/max_short/max_total from a profile object.
    Fields a mode does not use default to 0; fields it needs are required."""
    def get(name, needed, key=None):
        key = key or name
        v = raw.get(key)
        if v is None and not needed:
            return 0
        return _int(v, f"{p}.{key}", 0, 1000)

    max_spot = get("max_spot", need_spot)
    max_long = get("max_long", need_futures)
    max_short = get("max_short", need_futures)
    total_key = "max_futures_total" if "max_futures_total" in raw else "max_total"
    if need_futures:
        total = _int(raw.get(total_key), f"{p}.{total_key}", 0, 2000)
    else:
        total = 0
    if total > max_long + max_short:
        raise PayloadRejected(f"{p}.{total_key} cannot exceed max_long + max_short")
    if need_futures and total < max(max_long, max_short):
        raise PayloadRejected(f"{p}.{total_key} must be >= max(max_long, max_short)")
    return {"max_spot": max_spot, "max_long": max_long, "max_short": max_short, "max_total": total}


def _validate_profiles(raw_profiles) -> dict:
    if not isinstance(raw_profiles, dict):
        raise PayloadRejected("profiles missing")
    for m in MODES:
        if not isinstance(raw_profiles.get(m), dict):
            raise PayloadRejected(f"profiles.{m} missing or not an object")

    out = {
        "spot_only": _caps(raw_profiles["spot_only"], "profiles.spot_only", need_spot=True),
        "futures_only": _caps(raw_profiles["futures_only"], "profiles.futures_only", need_futures=True),
    }
    # Standalone profiles never carry the other market's slots.
    if out["spot_only"]["max_long"] or out["spot_only"]["max_short"] or out["spot_only"]["max_total"]:
        raise PayloadRejected("profiles.spot_only: futures limits must be zero")
    if out["futures_only"]["max_spot"]:
        raise PayloadRejected("profiles.futures_only: max_spot must be zero")

    raw_both = raw_profiles["both"]
    both = _caps(raw_both, "profiles.both", need_spot=True, need_futures=True)
    if str(raw_both.get("futures_margin_mode") or "").strip().lower() != "isolated":
        raise PayloadRejected("profiles.both.futures_margin_mode must be isolated")
    if str(raw_both.get("required_account_mode") or "").strip().lower() != "unified":
        raise PayloadRejected("profiles.both.required_account_mode must be unified")
    budget = raw_both.get("market_budget")
    if not isinstance(budget, dict):
        raise PayloadRejected("profiles.both.market_budget missing")
    p = "profiles.both.market_budget"
    spot_pct = _dec(budget.get("spot_pct"), f"{p}.spot_pct", 0, 100)
    fut_pct = _dec(budget.get("futures_margin_pct"), f"{p}.futures_margin_pct", 0, 100)
    min_reserve = _dec(budget.get("minimum_reserve_pct"), f"{p}.minimum_reserve_pct", 0, 100)
    if spot_pct + fut_pct + min_reserve - _HUNDRED > _PCT_TOLERANCE:
        raise PayloadRejected(f"{p}: spot_pct + futures_margin_pct + minimum_reserve_pct exceeds 100")
    both.update(
        spot_pct=str(spot_pct),
        futures_pct=str(fut_pct),
        reserve_pct=str(min_reserve),
        fundable_spot_max=_int(raw_both.get("fundable_spot_max"), "profiles.both.fundable_spot_max", 0, 1000),
        fundable_futures_total_max=_int(
            raw_both.get("fundable_futures_total_max"), "profiles.both.fundable_futures_total_max", 0, 2000
        ),
        spot_order_pct=str(_dec(raw_both.get("spot_order_pct"), "profiles.both.spot_order_pct", Decimal("0.01"), 100)),
        futures_margin_pct=str(
            _dec(raw_both.get("futures_margin_pct"), "profiles.both.futures_margin_pct", Decimal("0.01"), 100)
        ),
    )
    out["both"] = both
    return out


def validate_payload(payload, now_ms: Optional[int] = None) -> dict:
    """Validates a schema-9 risk heartbeat. Returns the normalized fields used
    to build a MarketRiskSnapshot; raises PayloadRejected otherwise."""
    now_ms = _now_ms() if now_ms is None else now_ms
    if not isinstance(payload, dict):
        raise PayloadRejected("Payload must be a JSON object")
    if payload.get("action") != "update_market_risk":
        raise PayloadRejected("action must be update_market_risk")
    if _int(payload.get("schema_version"), "schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise PayloadRejected(f"Unsupported schema_version (expected {SUPPORTED_SCHEMA_VERSION})")
    if str(payload.get("exchange") or "").strip().lower() != "hyperliquid":
        raise PayloadRejected("exchange must be HyperLiquid")
    if payload.get("event_type") != "risk_heartbeat":
        raise PayloadRejected("event_type must be risk_heartbeat")
    risk_event_type = payload.get("risk_event_type")
    if risk_event_type not in RISK_EVENT_TYPES:
        raise PayloadRejected(f"risk_event_type must be one of {RISK_EVENT_TYPES}")
    reason = payload.get("heartbeat_reason")
    if reason not in HEARTBEAT_REASONS:
        raise PayloadRejected(f"heartbeat_reason must be one of {HEARTBEAT_REASONS}")

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
        raise PayloadRejected("event_id must be a non-empty string (<=128 chars)")

    bar_ms = _int(payload.get("bar_close_time_ms"), "bar_close_time_ms", 1)
    if bar_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        raise PayloadRejected("bar_close_time_ms is in the future")
    stale_after = (
        _int(payload["stale_after_sec"], "stale_after_sec", 60, settings.HL_MAX_STALE_AFTER_SEC)
        if payload.get("stale_after_sec") is not None
        else settings.HL_DEFAULT_STALE_AFTER_SEC
    )

    data_ok = payload.get("data_ok")
    if not isinstance(data_ok, bool):
        raise PayloadRejected("data_ok is a required boolean")
    health = payload.get("data_health")
    if health is None:
        health = {}
    if not isinstance(health, dict):
        raise PayloadRejected("data_health must be an object")
    health = dict(health)
    health["data_ok"] = data_ok

    alt = payload.get("alt_market")
    if not isinstance(alt, dict):
        raise PayloadRejected("alt_market missing")
    regime_key = alt.get("regime")
    if regime_key not in REGIMES:  # never the legacy root `regime` label
        raise PayloadRejected(f"alt_market.regime must be one of {tuple(REGIMES)}")

    profiles = _validate_profiles(payload.get("profiles"))

    hints = payload.get("order_hints")
    if hints is not None and not isinstance(hints, dict):
        raise PayloadRejected("order_hints must be an object")
    hints = hints or {}
    both = profiles["both"]
    spot_order_pct = _dec(hints.get("spot_pct", both["spot_order_pct"]), "order_hints.spot_pct", Decimal("0.01"), 100)
    fut_margin_pct = _dec(
        hints.get("futures_margin_pct", both["futures_margin_pct"]),
        "order_hints.futures_margin_pct", Decimal("0.01"), 100,
    )
    leverage = _int(
        hints.get("max_leverage", DEFAULT_LEVERAGE), "order_hints.max_leverage", 1, settings.HL_HARD_MAX_LEVERAGE
    )
    if str(hints.get("futures_margin_mode", "isolated")).strip().lower() != "isolated":
        raise PayloadRejected("order_hints.futures_margin_mode must be isolated")

    return {
        "event_id": event_id.strip(),
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "release_version": str(payload.get("release_version") or "")[:64],
        "controller_version": str(payload.get("controller_version") or "")[:64],
        "event_type": f"{payload['event_type']}:{risk_event_type}:{reason}"[:50],
        # Receipt time is the freshness clock; the bar close orders events.
        "event_time_ms": now_ms,
        "primary_state_time_ms": bar_ms,
        "stale_after_sec": stale_after,
        "regime_key": regime_key,
        "regime_value": REGIMES[regime_key],
        "data_health": health,
        "allocation_profiles": profiles,
        "spot_trade_pct_of_pool": spot_order_pct,
        "futures_margin_pct_of_pool": fut_margin_pct,
        "futures_leverage": leverage,
    }


def ingest_payload(payload, now_ms: Optional[int] = None) -> MarketRiskSnapshot:
    """Validates and atomically stores a heartbeat.

    Raises DuplicateEvent for an already-accepted event_id and PayloadRejected
    for malformed or out-of-order messages. Snapshots are append-only and
    "current" is the max by (bar_close_time_ms, receipt time), so an older bar
    can never replace a newer accepted state. A same-bar heartbeat (e.g. a
    restart replay or deadman refresh) is accepted as a newer refresh."""
    now_ms = _now_ms() if now_ms is None else now_ms
    fields = validate_payload(payload, now_ms)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_INGEST_LOCK_NAMESPACE, 1])
        if MarketRiskSnapshot.objects.filter(event_id=fields["event_id"]).exists():
            raise DuplicateEvent(f"Duplicate event_id {fields['event_id']}")
        latest = MarketRiskSnapshot.objects.first()
        if latest is not None:
            if fields["primary_state_time_ms"] < latest.primary_state_time_ms:
                raise PayloadRejected("Out-of-order: bar_close_time_ms older than accepted state")
            if fields["event_time_ms"] <= latest.event_time_ms:
                fields["event_time_ms"] = latest.event_time_ms + 1
        return MarketRiskSnapshot.objects.create(raw_payload=payload, **fields)


def get_current_snapshot() -> Optional[MarketRiskSnapshot]:
    return MarketRiskSnapshot.objects.first()


def snapshot_is_fresh(snapshot, now_ms: Optional[int] = None) -> bool:
    """Backend-enforced expiry: independent of any TradingView follow-up."""
    if snapshot is None:
        return False
    now_ms = _now_ms() if now_ms is None else now_ms
    return now_ms <= snapshot.event_time_ms + snapshot.stale_after_sec * 1000


def _timeframe_invalid(health) -> bool:
    if not isinstance(health, dict):
        return False
    return any(health.get(k) is False for k in _TF_OK_KEYS) or any(health.get(k) is True for k in _TF_BAD_KEYS)


# --------------------------------------------------------------------------
# Decision object
# --------------------------------------------------------------------------

@dataclass
class EntryDecision:
    allowed: bool
    reason: str = ""
    market: str = ""
    mode: str = ""
    snapshot: Optional[MarketRiskSnapshot] = None
    profile: dict = field(default_factory=dict)
    effective_pct: Optional[Decimal] = None  # Both pool % for the market; None = no pool
    reserve_pct: Decimal = Decimal(0)
    leverage: int = 1
    trade_pct: Decimal = Decimal(0)
    budget: Optional[Decimal] = None  # spot quote amount / futures initial margin
    notional: Optional[Decimal] = None
    max_spot: int = 0
    max_long: int = 0
    max_short: int = 0
    max_total: int = 0
    fundable_spot: Optional[int] = None
    fundable_futures: Optional[int] = None
    reservation: Optional[HLOrderReservation] = None

    # --- reservation lifecycle (see HLOrderReservation) -------------------
    def mark_submitting(self):
        """Call immediately before the exchange order call."""
        _set_reservation(self.reservation, HLOrderReservation.Status.SUBMITTING)

    def confirm(self, order_id=""):
        """Call once the order/position row exists: the row now carries the
        commitment, so the reservation stops counting."""
        _set_reservation(self.reservation, HLOrderReservation.Status.CONFIRMED, order_id=str(order_id or ""))

    def release(self, note=""):
        _set_reservation(self.reservation, HLOrderReservation.Status.RELEASED, note=note)


def _set_reservation(res, status, order_id=None, note=None):
    if res is None:
        return
    res.status = status
    fields = ["status", "updated_at"]
    if order_id is not None:
        res.order_id = order_id[:100]
        fields.append("order_id")
    if note is not None:
        res.note = note[:255]
        fields.append("note")
    res.save(update_fields=fields)


class reservation_scope:
    """Context manager guaranteeing a reservation never leaks:

    - exit before the exchange call (PENDING)        -> RELEASED
    - exchange rejected the order (ccxt ExchangeError that is not a network
      problem)                                       -> RELEASED
    - ambiguous failure (timeout/network/other) or an unconfirmed
      submission                                     -> UNCERTAIN (held)
    - confirmed (row created)                        -> left CONFIRMED
    An uncertain submission is never released on a guess.

    Any Exception is captured in `.error` and suppressed so the surrounding
    transaction COMMITS the reservation state (and any rows already created)
    instead of rolling it back; the caller must check `.error` afterwards."""

    def __init__(self, decision):
        self.decision = decision
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        import ccxt

        res = self.decision.reservation
        S = HLOrderReservation.Status
        if res is not None:
            if res.status == S.PENDING:
                _set_reservation(res, S.RELEASED, note="not submitted")
            elif res.status == S.SUBMITTING:
                definite_reject = (
                    exc is not None
                    and isinstance(exc, ccxt.ExchangeError)
                    and not isinstance(exc, ccxt.NetworkError)
                )
                if definite_reject:
                    _set_reservation(res, S.RELEASED, note=f"exchange rejected: {str(exc)[:200]}")
                else:
                    _set_reservation(res, S.UNCERTAIN, note="submission outcome unknown; reconcile with exchange")
                    logger.error("HL reservation %s left UNCERTAIN (%r)", res.pk, exc)
        if exc is not None and isinstance(exc, Exception):
            self.error = exc
            return True
        return False


def release_stale_pending_reservations(user=None, now=None) -> int:
    """Releases PENDING reservations that never reached the exchange call
    (worker died before submitting). UNCERTAIN ones are never auto-released."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.HL_PENDING_RESERVATION_TTL_SEC)
    qs = HLOrderReservation.objects.filter(status=HLOrderReservation.Status.PENDING, created_at__lt=cutoff)
    if user is not None:
        qs = qs.filter(user=user)
    return qs.update(status=HLOrderReservation.Status.RELEASED, note="stale pending: never submitted")


def _reject(reason, **kw) -> EntryDecision:
    return EntryDecision(allowed=False, reason=reason, **kw)


HL = "HYPERLIQUID"
_ACTIVE_ORDER_STATUSES = lambda model: [model.TradeStatus.POSITION, model.TradeStatus.OPEN]


def _hl_positions(model, user):
    return model.objects.filter(user=user, exchange=HL, status__in=_ACTIVE_ORDER_STATUSES(model))


def _active_reservations(user):
    return HLOrderReservation.objects.filter(user=user, status__in=HLOrderReservation.ACTIVE)


def _spot_committed(user) -> Decimal:
    total = Decimal(0)
    for o in _hl_positions(SpotOrder, user):
        qty = o.final_quantity or o.order_quantity or 0
        total += Decimal(qty) * Decimal(o.entry_price or 0)
    for r in _active_reservations(user).filter(market="spot"):
        total += r.amount_usdc
    return total


def _futures_rows(user):
    return list(_hl_positions(FutureOrder, user))


def _futures_committed_margin(user, rows=None) -> Decimal:
    total = Decimal(0)
    for o in rows if rows is not None else _futures_rows(user):
        lev = Decimal(max(o.leverage or 1, 1))
        total += Decimal(o.order_quantity or 0) * Decimal(o.entry_price or 0) / lev
    for r in _active_reservations(user).filter(market="futures"):
        total += r.amount_usdc
    return total


def _futures_gross_notional(user, rows=None) -> Decimal:
    total = Decimal(0)
    for o in rows if rows is not None else _futures_rows(user):
        total += Decimal(o.order_quantity or 0) * Decimal(o.entry_price or 0)
    for r in _active_reservations(user).filter(market="futures"):
        total += r.notional_usdc
    return total


def _futures_stop_risk(user, rows=None) -> Decimal:
    """Aggregate USDC at risk if every futures stop is hit: qty x stop distance
    plus an entry+exit cost buffer. A position with no valid stop is counted at
    its full isolated margin (its maximum possible loss)."""
    buf = Decimal(str(settings.HL_ENTRY_COST_BUFFER_PCT)) / _HUNDRED
    total = Decimal(0)
    for o in rows if rows is not None else _futures_rows(user):
        qty = Decimal(o.order_quantity or 0)
        entry = Decimal(o.entry_price or 0)
        sl = Decimal(o.stop_loss_price or 0)
        lev = Decimal(max(o.leverage or 1, 1))
        if sl > 0 and o.stop_loss_status == FutureOrder.TradeStatus.POSITION:
            total += qty * abs(entry - sl) + 2 * buf * qty * entry
        else:
            total += qty * entry / lev
    for r in _active_reservations(user).filter(market="futures"):
        total += r.stop_risk_usdc
    return total


def _counts(user, rows=None):
    """Deduplicated open + pending slot counts: order rows, plus reservations
    that have no row yet (CONFIRMED/RELEASED reservations are excluded)."""
    rows = rows if rows is not None else _futures_rows(user)
    spot = _hl_positions(SpotOrder, user).count()
    long_ = sum(1 for o in rows if o.direction == FutureOrder.TradeDirection.LONG)
    short = sum(1 for o in rows if o.direction == FutureOrder.TradeDirection.SHORT)
    for r in _active_reservations(user):
        if r.market == "spot":
            spot += 1
        elif r.direction == FutureOrder.TradeDirection.LONG:
            long_ += 1
        else:
            short += 1
    return spot, long_, short


def fundable_slots(spot_pct: Decimal, fut_pct: Decimal, spot_order_pct: Decimal, fut_margin_pct: Decimal, profile: dict):
    """V4.3.1 fundable slots for Both/Unified (percentage points throughout)."""
    fs = min(profile["max_spot"], profile["fundable_spot_max"], math.floor(spot_pct / spot_order_pct))
    ff = min(
        profile["max_long"] + profile["max_short"],
        profile["max_total"],
        profile["fundable_futures_total_max"],
        math.floor(fut_pct / fut_margin_pct),
    )
    return int(fs), int(ff)


# --------------------------------------------------------------------------
# Per-member gate
# --------------------------------------------------------------------------

def evaluate_entry(user, market: str, direction: Optional[str] = None, symbol: str = "") -> EntryDecision:
    """Slot-and-permission gate for a new risk-increasing HyperLiquid entry.
    `market` is "spot" or "futures"; `direction` ("LONG"/"SHORT") matters for
    futures. Fails closed on any missing/invalid/stale input. Does not look at
    capital (see size_entry)."""
    ts = TradeSettings.get_for_user(user)
    key = UserHyperLiquidKey.objects.filter(user=user).first()
    mode = key.trading_mode if key else ""
    base = dict(market=market, mode=mode)
    if settings.HL_KILL_SWITCH:
        return _reject("HyperLiquid kill switch is on: new entries blocked", **base)
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
    health = snap.data_health or {}
    if health.get("data_ok") is not True:
        return _reject("Controller reports data_ok=false: new entries blocked", **base)
    if _timeframe_invalid(health):
        return _reject("Controller reports an invalid chart timeframe: new entries blocked", **base)

    profile = (snap.allocation_profiles or {}).get(mode)
    if not isinstance(profile, dict):
        return _reject(f"Snapshot has no profile for mode {mode}", **base)
    base["profile"] = profile

    try:
        caps = {k: int(profile[k]) for k in ("max_spot", "max_long", "max_short", "max_total")}
        if mode == "both":
            spot_pct = Decimal(profile["spot_pct"])
            fut_pct = Decimal(profile["futures_pct"])
            reserve = Decimal(profile["reserve_pct"])
            spot_order = Decimal(profile["spot_order_pct"])
            fut_margin = Decimal(profile["futures_margin_pct"])
            base["reserve_pct"] = reserve
            fs, ff = fundable_slots(spot_pct, fut_pct, spot_order, fut_margin, profile)
            base.update(fundable_spot=fs, fundable_futures=ff)
            base["effective_pct"] = spot_pct if market == "spot" else fut_pct
    except (KeyError, InvalidOperation, ValueError, TypeError, ZeroDivisionError):
        return _reject("Snapshot profile is invalid", **base)

    hard_spot = settings.HL_HARD_MAX_SPOT_POSITIONS
    hard_fut = settings.HL_HARD_MAX_FUTURES_POSITIONS
    if ts.hl_sync_from_webhook:
        max_spot = caps["max_spot"]
        max_long, max_short, max_total = caps["max_long"], caps["max_short"], caps["max_total"]
    else:
        # Auto-update off: member's manual caps apply (never above the
        # backend hard limits; the heartbeat still gates freshness/mode).
        max_spot = ts.hl_spot_max_positions
        max_long, max_short, max_total = (
            ts.hl_futures_max_long, ts.hl_futures_max_short, ts.hl_futures_max_positions
        )
    max_spot = min(max_spot, hard_spot)
    max_total = min(max_total, hard_fut)
    max_long = min(max_long, max_total)
    max_short = min(max_short, max_total)
    base.update(max_spot=max_spot, max_long=max_long, max_short=max_short, max_total=max_total)

    rows = _futures_rows(user)
    spot_n, long_n, short_n = _counts(user, rows)

    if market == "spot":
        if mode == "both" and base["effective_pct"] <= 0:
            return _reject("Effective Both/Unified spot pool is 0%: no new spot entries", **base)
        if spot_n >= max_spot:
            return _reject(f"Max HyperLiquid spot positions reached ({max_spot})", **base)
        if mode == "both" and spot_n >= base["fundable_spot"]:
            return _reject(f"Fundable spot slots reached ({base['fundable_spot']})", **base)
        base["trade_pct"] = (
            snap.spot_trade_pct_of_pool if ts.hl_sync_from_webhook else ts.hl_spot_trade_pct
        )
    else:
        if mode == "both" and base["effective_pct"] <= 0:
            return _reject("Effective Both/Unified futures pool is 0%: no new futures entries", **base)
        open_f = long_n + short_n
        if open_f >= max_total:
            return _reject(f"Max HyperLiquid futures positions reached ({max_total})", **base)
        if direction == FutureOrder.TradeDirection.LONG:
            if long_n >= max_long:
                return _reject(f"Max HyperLiquid LONG futures positions reached ({max_long})", **base)
        else:
            if short_n >= max_short:
                return _reject(f"Max HyperLiquid SHORT futures positions reached ({max_short})", **base)
        if mode == "both" and open_f >= base["fundable_futures"]:
            return _reject(f"Fundable futures slots reached ({base['fundable_futures']})", **base)
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


# --------------------------------------------------------------------------
# Equity baseline + sizing
# --------------------------------------------------------------------------

def get_policy_baseline(user, snapshot, live_equity: Decimal) -> HLEquityBaseline:
    """policyBaselineEquityUSDC: frozen between regime-policy boundaries.
    ASSUMPTION: the boundary is a change of the canonical ALT regime (the
    brief does not give the exact Pine rule). Heartbeats alone never refresh
    it."""
    base = HLEquityBaseline.objects.select_for_update().filter(user=user).first()
    if base is None:
        return HLEquityBaseline.objects.create(
            user=user, baseline_equity=live_equity, regime_key=snapshot.regime_key,
            refresh_reason="initial", event_id=snapshot.event_id,
        )
    if base.regime_key != snapshot.regime_key:
        base.refresh_reason = f"regime_change:{base.regime_key}->{snapshot.regime_key}"
        base.regime_key = snapshot.regime_key
        base.baseline_equity = live_equity
        base.event_id = snapshot.event_id
        base.save()
    return base


def size_entry(
    user, market, free_usdc, direction=None, symbol="", *,
    equity_usdc=None, entry_price=None, sl_price=None, reserve=True,
) -> EntryDecision:
    """Full gate + sizing + reservation. Must run under user_trade_open_lock
    (inside a transaction), after fetching the live exchange balances.

    free_usdc    venue-reported available USDC (exchange holds already netted)
    equity_usdc  venue-reported account equity at entry (sizingEquityUSDC)
    On success `decision.budget` is the USDC to commit (spot notional, or
    futures *initial margin*; futures notional = budget x decision.leverage)
    and, when reserve=True, a PENDING HLOrderReservation exists: callers must
    use reservation_scope(decision) and call mark_submitting()/confirm().

    Never subtracts the same commitment twice: free_usdc already reflects
    exchange-recognised holds, only unreflected local reservations are
    deducted. Any missing exchange figure rejects the entry."""
    d = evaluate_entry(user, market, direction, symbol)
    if not d.allowed:
        log_decision(user, d, symbol, direction or "")
        return d

    def fail(reason):
        d.allowed = False
        d.reason = reason
        d.budget = None
        log_decision(user, d, symbol, direction or "")
        return d

    try:
        free = Decimal(str(free_usdc))
        equity = Decimal(str(equity_usdc))
    except (InvalidOperation, ValueError, TypeError):
        return fail("Exchange balance/equity unavailable: refusing risk-increasing order")
    if not free.is_finite() or not equity.is_finite() or equity <= 0 or free < 0:
        return fail("Exchange balance/equity unavailable: refusing risk-increasing order")

    release_stale_pending_reservations(user)
    snap = d.snapshot
    is_spot = market == "spot"
    cost_buf = Decimal(str(settings.HL_ENTRY_COST_BUFFER_PCT)) / _HUNDRED

    # Nominal size is a % of *entry equity* (never of shrinking free balance).
    candidate = equity * d.trade_pct / _HUNDRED
    notional = candidate if is_spot else candidate * d.leverage
    buffer = notional * cost_buf

    # Local holds not yet reflected in the venue-reported available balance.
    local_holds = sum((r.amount_usdc for r in _active_reservations(user)), Decimal(0))
    spendable = free - local_holds

    baseline = get_policy_baseline(user, snap, equity)
    reserve_floor = Decimal(str(settings.HL_MIN_RESERVE_PCT)) / _HUNDRED * max(equity, baseline.baseline_equity)
    if spendable - candidate - buffer < reserve_floor:
        return fail(
            f"Live reserve: free {spendable:.2f} - order {candidate:.2f} - costs {buffer:.2f} "
            f"would fall below the {reserve_floor:.2f} USDC minimum reserve"
        )

    rows = None if is_spot else _futures_rows(user)
    if d.mode == "both":
        pool_pct = d.effective_pct
        pool = baseline.baseline_equity * pool_pct / _HUNDRED
        committed = _spot_committed(user) if is_spot else _futures_committed_margin(user, rows)
        if committed + candidate > pool:
            return fail(
                f"Both/Unified {market} pool exhausted (pool {pool:.2f}, committed {committed:.2f}, "
                f"order {candidate:.2f})"
            )

    stop_risk = Decimal(0)
    if not is_spot:
        gross_cap = Decimal(str(settings.HL_MAX_GROSS_FUTURES_NOTIONAL_PCT)) / _HUNDRED * equity
        gross = _futures_gross_notional(user, rows)
        if gross + notional > gross_cap:
            return fail(
                f"Gross futures notional cap: {gross:.2f} + {notional:.2f} > {gross_cap:.2f} "
                f"({settings.HL_MAX_GROSS_FUTURES_NOTIONAL_PCT}% of equity)"
            )
        risk_pct = Decimal(str(settings.HL_MAX_AGG_STOP_RISK_PCT))
        if risk_pct > 0:
            if sl_price is None or entry_price is None or Decimal(str(sl_price)) <= 0:
                return fail("No valid stop price for the new futures entry: cannot estimate stop risk")
            ep, sp = Decimal(str(entry_price)), Decimal(str(sl_price))
            wrong_side = (
                sp >= ep if direction == FutureOrder.TradeDirection.LONG else sp <= ep
            )
            if wrong_side:
                return fail("Stop price is on the wrong side of entry: cannot estimate stop risk")
            stop_risk = notional / ep * abs(ep - sp) + 2 * buffer
            cap = risk_pct / _HUNDRED * equity
            current_risk = _futures_stop_risk(user, rows)
            if current_risk + stop_risk > cap:
                return fail(
                    f"Aggregate stop risk {current_risk:.2f} + {stop_risk:.2f} > {cap:.2f} "
                    f"({risk_pct}% of equity)"
                )

    d.budget = candidate
    d.notional = notional
    if reserve:
        d.reservation = HLOrderReservation.objects.create(
            user=user, market=market, direction=(direction or "") if not is_spot else "",
            symbol=symbol[:30], amount_usdc=candidate, notional_usdc=notional,
            stop_risk_usdc=stop_risk, event_id=snap.event_id,
        )
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
