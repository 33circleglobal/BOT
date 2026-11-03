"""Throneex HyperLiquid V4.3.1 (schema 9) risk-controller tests.

NOTE: payloads here follow the integration brief, not a captured TradingView
alert (the Pine script / reference JS were unavailable)."""
import copy
import json
import threading
import time
from decimal import Decimal
from unittest.mock import patch

import ccxt
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings

from apps.accounts.models import UserHyperLiquidKey
from apps.trade.models import (
    FutureOrder,
    HLEquityBaseline,
    HLOrderReservation,
    MarketRiskSnapshot,
    RiskDecisionLog,
    SpotOrder,
    TradeSettings,
    WebhookLog,
)
from apps.trade.utils import market_risk
from apps.trade.utils.locks import user_trade_open_lock
from apps.trade.utils.market_risk import EntryDecision, reservation_scope, size_entry
from apps.trade.utils.risk_guard import can_open_futures_position, can_open_spot_position

User = get_user_model()
HL = "HYPERLIQUID"
SECRET = "s3cret"
S = HLOrderReservation.Status

# alt regime -> (spot pool %, futures pool %, reserve %)  [brief section 5]
POLICY = {
    "strong_bull": (50, 30, 20),
    "bull": (40, 40, 20),
    "neutral": (20, 30, 50),
    "bear": (0, 40, 60),
    "strong_bear": (0, 60, 40),
}


def make_payload(regime="strong_bull", max_spot=10, max_long=5, max_short=2, **over):
    now = int(time.time() * 1000)
    sp, fp, rs = POLICY[regime]
    fut_total = max_long + max_short
    fs = min(max_spot, sp // 15)
    ff = min(fut_total, fp // 10)
    p = {
        "action": "update_market_risk",
        "controller": "hyperliquid_trade_slots",
        "exchange": "Hyperliquid",
        "schema_version": 9,
        "release_version": "4.3.1",
        "controller_version": "THRONEEX-HL-BINANCE-CORE-V4.3.1",
        "event_type": "risk_heartbeat",
        "risk_event_type": "heartbeat",
        "heartbeat_reason": "primary_period",
        "event_id": f"EVT-{regime}-{now}",
        "bar_close_time_ms": now - 600000,
        "stale_after_sec": 5400,
        "data_ok": True,
        "data_health": {"alt_ok": True, "btc_ok": True, "btcd_ok": True, "others_ok": True},
        "regime": "NEUTRAL / MIXED",  # legacy combined label: must never drive logic
        "alt_market": {"symbol": "CRYPTOCAP:TOTAL3ES", "regime": regime, "regime_value": 0},
        "profiles": {
            "spot_only": {"max_spot": max_spot, "max_long": 0, "max_short": 0, "max_total": max_spot},
            "futures_only": {"max_spot": 0, "max_long": max_long, "max_short": max_short, "max_total": fut_total},
            "both": {
                "max_spot": max_spot, "max_long": max_long, "max_short": max_short,
                "max_futures_total": fut_total,
                "market_budget": {"spot_pct": sp, "futures_margin_pct": fp, "minimum_reserve_pct": rs},
                "fundable_spot_max": fs, "fundable_futures_total_max": ff,
                "spot_order_pct": 15, "futures_margin_pct": 10,
                "futures_margin_mode": "isolated", "required_account_mode": "unified",
            },
        },
        "order_hints": {"spot_pct": 15, "futures_margin_pct": 10, "max_leverage": 3,
                        "futures_margin_mode": "isolated"},
    }
    p.update(over)
    return p


_n = [0]


def fo(user, **kw):
    _n[0] += 1
    kw.setdefault("order_id", f"F-{_n[0]}")
    kw.setdefault("stop_loss_order_id", f"SL-{_n[0]}")
    kw.setdefault("symbol", f"X{_n[0]}")
    kw.setdefault("exchange", HL)
    kw.setdefault("status", "POSITION")
    return FutureOrder.objects.create(user=user, **kw)


def so(user, **kw):
    _n[0] += 1
    kw.setdefault("order_id", f"S-{_n[0]}")
    kw.setdefault("symbol", f"Y{_n[0]}")
    kw.setdefault("exchange", HL)
    kw.setdefault("status", "POSITION")
    kw.setdefault("direction", "LONG")
    return SpotOrder.objects.create(user=user, **kw)


def add_hl_key(user, mode="both"):
    key = UserHyperLiquidKey(user=user, trading_mode=mode)
    key.set_master_wallet_address("0xmaster")
    key.set_api_wallet_address("0xapi")
    key.set_api_private_key("pk")
    key.save()
    return key


def post(client, payload, secret=SECRET):
    url = "/webhook/" + (f"?secret={secret}" if secret else "")
    return client.post(url, data=json.dumps(payload), content_type="application/json")


# --------------------------------------------------------------------------
# H. Webhook
# --------------------------------------------------------------------------

@override_settings(TRADINGVIEW_WEBHOOK_SECRET=SECRET)
class WebhookIngestTests(TestCase):
    def setUp(self):
        self.client = Client()

    def reject(self, payload, code=400):
        r = post(self.client, payload)
        self.assertEqual(r.status_code, code, r.content)
        self.assertEqual(MarketRiskSnapshot.objects.count(), 0)

    def test_valid_payload_accepted_and_persisted(self):
        p = make_payload()
        r = post(self.client, p)
        self.assertEqual(r.status_code, 200, r.content)
        snap = MarketRiskSnapshot.objects.get()
        self.assertEqual(snap.event_id, p["event_id"])
        self.assertEqual(snap.schema_version, 9)
        self.assertEqual(snap.regime_key, "strong_bull")  # alt_market.regime, not root label
        self.assertEqual(snap.primary_state_time_ms, p["bar_close_time_ms"])
        both = snap.allocation_profiles["both"]
        self.assertEqual((both["spot_pct"], both["futures_pct"], both["reserve_pct"]), ("50", "30", "20"))
        self.assertEqual((both["max_spot"], both["max_long"], both["max_short"], both["max_total"]), (10, 5, 2, 7))
        self.assertEqual((both["fundable_spot_max"], both["fundable_futures_total_max"]), (3, 3))
        self.assertEqual(snap.allocation_profiles["spot_only"]["max_spot"], 10)
        self.assertEqual(snap.allocation_profiles["futures_only"]["max_total"], 7)
        self.assertEqual(snap.raw_payload["event_id"], p["event_id"])
        self.assertEqual(WebhookLog.objects.get().event_id, p["event_id"])

    def test_spec_example_payload_shape_accepted(self):
        """The shape from the brief's example (budget/fundable under 'both')."""
        p = make_payload(max_spot=8)
        self.assertEqual(post(self.client, p).status_code, 200)

    def test_invalid_authentication(self):
        self.assertEqual(post(self.client, make_payload(), secret="").status_code, 401)
        self.assertEqual(post(self.client, make_payload(), secret="nope").status_code, 401)
        self.assertEqual(MarketRiskSnapshot.objects.count(), 0)
        with override_settings(TRADINGVIEW_WEBHOOK_SECRET=""):
            self.assertEqual(post(self.client, make_payload(), secret="").status_code, 200)

    def test_header_secret_accepted(self):
        r = self.client.post("/webhook/", data=json.dumps(make_payload()),
                             content_type="application/json", HTTP_X_WEBHOOK_SECRET=SECRET)
        self.assertEqual(r.status_code, 200)

    def test_wrong_schema_action_event_type(self):
        self.reject(make_payload(schema_version=5))
        self.reject(make_payload(schema_version=8))
        self.reject(make_payload(schema_version="nine"))
        self.reject(make_payload(action="place_order"))
        self.reject(make_payload(event_type="trade_signal"))
        self.reject(make_payload(risk_event_type="weird"))
        self.reject(make_payload(heartbeat_reason="whatever"))

    def test_wrong_exchange_is_not_hyperliquid(self):
        # Another exchange never reaches the HL parser (legacy Binance path).
        self.assertFalse(market_risk.is_market_risk_payload({"exchange": "Binance", "schema_version": 9}))
        self.assertTrue(market_risk.is_market_risk_payload({"exchange": "Hyperliquid"}))
        with self.assertRaises(market_risk.PayloadRejected):
            market_risk.validate_payload(make_payload(exchange="Binance"))

    def test_regime_must_be_canonical_alt_regime(self):
        p = make_payload()
        p["alt_market"]["regime"] = "NEUTRAL / MIXED"
        self.reject(p)
        p = make_payload()
        del p["alt_market"]
        self.reject(p)

    def test_missing_required_fields(self):
        for key in ("event_id", "bar_close_time_ms", "data_ok", "profiles", "heartbeat_reason", "risk_event_type"):
            p = make_payload()
            del p[key]
            self.reject(p)

    def test_missing_or_incomplete_profile(self):
        for mutate in (
            lambda p: p["profiles"].pop("both"),
            lambda p: p["profiles"].pop("spot_only"),
            lambda p: p["profiles"].pop("futures_only"),
            lambda p: p["profiles"]["spot_only"].pop("max_spot"),
            lambda p: p["profiles"]["futures_only"].pop("max_long"),
            lambda p: p["profiles"]["both"].pop("market_budget"),
            lambda p: p["profiles"]["both"].pop("fundable_spot_max"),
            lambda p: p["profiles"]["both"].pop("fundable_futures_total_max"),
        ):
            p = make_payload()
            mutate(p)
            self.reject(p)

    def test_invalid_limits_and_budget(self):
        for mutate in (
            lambda p: p["profiles"]["both"].update(max_long=-1),
            lambda p: p["profiles"]["both"].update(max_long="x"),
            lambda p: p["profiles"]["both"].update(max_futures_total=9),  # > long + short
            lambda p: p["profiles"]["both"]["market_budget"].update(spot_pct=120),
            lambda p: p["profiles"]["both"]["market_budget"].update(spot_pct=70),  # sum > 100
            lambda p: p["profiles"]["both"]["market_budget"].update(spot_pct="abc"),
            lambda p: p["profiles"]["both"].update(futures_margin_mode="cross"),
            lambda p: p["profiles"]["both"].update(required_account_mode="standard"),
            lambda p: p["profiles"]["spot_only"].update(max_long=1),
            lambda p: p["profiles"]["futures_only"].update(max_spot=2),
            lambda p: p.update(stale_after_sec=0),
            lambda p: p.update(stale_after_sec=10**9),
            lambda p: p["order_hints"].update(max_leverage=50),
            lambda p: p["order_hints"].update(futures_margin_mode="cross"),
            lambda p: p.update(data_ok="yes"),
        ):
            p = make_payload()
            mutate(p)
            self.reject(p)

    def test_duplicate_event_id_is_idempotent(self):
        p = make_payload()
        self.assertEqual(post(self.client, p).status_code, 200)
        r = post(self.client, p)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ignored")
        self.assertEqual(MarketRiskSnapshot.objects.count(), 1)

    def test_out_of_order_bar_cannot_overwrite_newer(self):
        new = make_payload(event_id="NEW")
        post(self.client, new)
        old = make_payload(event_id="OLD", bar_close_time_ms=new["bar_close_time_ms"] - 1000)
        self.assertEqual(post(self.client, old).status_code, 400)
        self.assertEqual(market_risk.get_current_snapshot().event_id, "NEW")

    def test_restart_replay_same_bar_accepted_as_refresh(self):
        a = make_payload(event_id="A", heartbeat_reason="startup")
        b = make_payload(event_id="B", heartbeat_reason="deadman_refresh",
                         bar_close_time_ms=a["bar_close_time_ms"])
        self.assertEqual(post(self.client, a).status_code, 200)
        self.assertEqual(post(self.client, b).status_code, 200)
        self.assertEqual(market_risk.get_current_snapshot().event_id, "B")
        # ...and replaying A is a duplicate, never a rollback.
        self.assertEqual(post(self.client, a).json()["status"], "ignored")
        self.assertEqual(market_risk.get_current_snapshot().event_id, "B")

    def test_future_bar_rejected(self):
        self.reject(make_payload(bar_close_time_ms=int(time.time() * 1000) + 3600 * 1000))

    def test_data_ok_false_accepted_but_blocks_entries(self):
        u = User.objects.create_user("d", password="x")
        add_hl_key(u)
        p = make_payload(data_ok=False)
        # Conservative: a controller with ALT down must not grant bull pools.
        self.assertEqual(post(self.client, p).status_code, 200)
        d = market_risk.evaluate_entry(u, "spot")
        self.assertFalse(d.allowed)
        self.assertIn("data_ok=false", d.reason)

    def test_degraded_btc_btcd_others_feeds_do_not_block(self):
        u = User.objects.create_user("deg", password="x")
        add_hl_key(u)
        p = make_payload()
        p["data_health"].update(btc_ok=False, btcd_ok=False, others_ok=False)
        self.assertEqual(post(self.client, p).status_code, 200)
        self.assertTrue(market_risk.evaluate_entry(u, "spot").allowed)

    def test_invalid_chart_timeframe_blocks_entries(self):
        u = User.objects.create_user("tf", password="x")
        add_hl_key(u)
        p = make_payload()
        p["data_health"]["chart_timeframe_ok"] = False
        self.assertEqual(post(self.client, p).status_code, 200)
        d = market_risk.evaluate_entry(u, "spot")
        self.assertFalse(d.allowed)
        self.assertIn("timeframe", d.reason)

    def test_legacy_binance_update_market_risk_unchanged(self):
        u = User.objects.create_user("legacy", password="x")
        TradeSettings.get_for_user(u)
        r = self.client.post(
            "/webhook/",
            data=json.dumps({"action": "update_market_risk", "regime": "bear",
                             "futures": {"max_long": 1, "max_short": 4, "max_total": 5},
                             "spot": {"max_trades": 2}}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        ts = TradeSettings.objects.get(user=u)
        self.assertEqual((ts.futures_max_long, ts.futures_max_short, ts.futures_max_positions, ts.spot_max_positions),
                         (1, 4, 5, 2))
        self.assertEqual(MarketRiskSnapshot.objects.count(), 0)

    def test_binance_controller_payload_with_schema_version_uses_legacy_path(self):
        u = User.objects.create_user("bin", email="bin@x.com", password="x")
        TradeSettings.get_for_user(u)
        payload = {
            "spot": {"max_trades": 2}, "action": "update_market_risk",
            "target": {"spot": 2, "max_long": 1, "max_short": 2, "max_total": 3},
            "futures": {"max_long": 1, "max_short": 2, "max_total": 3},
            "event_id": "122ALT-PRIMARY-BTC-BREADTH-V2.2.2_1792722600000_neutral_bear_neutral_neutral",
            "exchange": "Binance", "strategy": "alt_primary_btc_overlay",
            "event_type": "market_state_change", "data_health": {"alt_ok": True, "btc_ok": True},
            "event_time_ms": 1792723500528, "event_sequence": 11333, "schema_version": 2,
            "release_version": "2.2.2", "stale_after_sec": 5400, "primary_state_time_ms": 1792722600000,
        }
        r = post(self.client, payload, secret="")
        self.assertEqual(r.status_code, 200, r.content)
        ts = TradeSettings.objects.get(user=u)
        self.assertEqual((ts.futures_max_long, ts.futures_max_short, ts.futures_max_positions, ts.spot_max_positions),
                         (1, 2, 3, 2))
        self.assertIsNotNone(ts.last_market_risk_update)
        self.assertEqual(MarketRiskSnapshot.objects.count(), 0)

    def test_hl_heartbeat_does_not_touch_binance_settings(self):
        u = User.objects.create_user("capuser", password="x")
        before = TradeSettings.get_for_user(u).futures_max_long
        post(self.client, make_payload())
        self.assertEqual(TradeSettings.objects.get(user=u).futures_max_long, before)


# --------------------------------------------------------------------------
# Gate / sizing helpers
# --------------------------------------------------------------------------

class HLBase(TestCase):
    mode = "both"

    def setUp(self):
        self.user = User.objects.create_user("hl", password="x")
        self.ts = TradeSettings.get_for_user(self.user)
        self.key = add_hl_key(self.user, self.mode)

    def snap(self, **kw):
        return market_risk.ingest_payload(make_payload(**kw))

    def set_mode(self, mode):
        UserHyperLiquidKey.objects.filter(pk=self.key.pk).update(trading_mode=mode)

    def spot(self, free=100, equity=100, **kw):
        return size_entry(self.user, "spot", free, None, "SYM", equity_usdc=equity, **kw)

    def fut(self, direction="LONG", free=100, equity=100, price=100, sl=None, **kw):
        if sl is None:
            sl = price * (0.97 if direction == "LONG" else 1.03)
        return size_entry(self.user, "futures", free, direction, "SYM", equity_usdc=equity,
                          entry_price=price, sl_price=sl, **kw)


# --------------------------------------------------------------------------
# A, B, E. Both / Unified funding policy
# --------------------------------------------------------------------------

class BothUnifiedTests(HLBase):
    def test_strong_bull_100_dollar_example(self):
        self.snap(max_spot=10, max_long=5, max_short=2)
        # Three spot entries of $15 (15% of $100) fit the $50 pool; the fourth does not.
        for _ in range(3):
            d = self.spot()
            self.assertTrue(d.allowed, d.reason)
            self.assertEqual(d.budget, Decimal(15))
        d = self.spot()
        self.assertFalse(d.allowed)
        self.assertIn("Fundable spot", d.reason)
        # Three futures entries of $10 margin / $30 notional; any L/S mix.
        for direction in ("LONG", "SHORT", "LONG"):
            d = self.fut(direction)
            self.assertTrue(d.allowed, d.reason)
            self.assertEqual((d.budget, d.notional, d.leverage), (Decimal(10), Decimal(30), 3))
        d = self.fut("LONG")
        self.assertFalse(d.allowed)
        self.assertIn("Fundable futures", d.reason)
        committed = sum(r.amount_usdc for r in HLOrderReservation.objects.filter(status=S.PENDING))
        self.assertEqual(committed, Decimal(75))
        self.assertEqual(Decimal(100) - committed, Decimal(25))  # remaining cash >= 20% reserve

    def test_theoretical_slots_per_regime(self):
        expected = {"strong_bull": (3, 3), "bull": (2, 4), "neutral": (1, 3), "bear": (0, 4), "strong_bear": (0, 6)}
        for regime, (fs, ff) in expected.items():
            both = market_risk.validate_payload(make_payload(regime=regime))["allocation_profiles"]["both"]
            got = market_risk.fundable_slots(
                Decimal(both["spot_pct"]), Decimal(both["futures_pct"]), Decimal(15), Decimal(10), both
            )
            self.assertEqual(got, (fs, ff), regime)

    def test_futures_pool_is_shared_not_split_by_direction(self):
        self.snap()
        # 3 longs (no reserved short slot) are fine...
        for _ in range(3):
            self.assertTrue(self.fut("LONG").allowed)
        # ...and the market short cap still applies independently.
        HLOrderReservation.objects.all().delete()
        self.snap(regime="strong_bear", max_long=5, max_short=1, event_id="E2")
        self.assertTrue(self.fut("SHORT").allowed)
        d = self.fut("SHORT")
        self.assertFalse(d.allowed)
        self.assertIn("SHORT", d.reason)

    def test_no_fixed_five_trade_ceiling(self):
        self.snap()
        n = sum(self.spot().allowed for _ in range(3)) + sum(self.fut("LONG").allowed for _ in range(3))
        self.assertEqual(n, 6)  # combined Spot + Futures exceeds an old "5" ceiling

    def test_bear_and_strong_bear_have_no_spot(self):
        for regime in ("bear", "strong_bear"):
            HLOrderReservation.objects.all().delete()
            self.snap(regime=regime, event_id=f"R-{regime}")
            d = self.spot()
            self.assertFalse(d.allowed, regime)
            self.assertIn("pool is 0%", d.reason)

    def test_strong_bear_gross_notional_cap_limits_to_four(self):
        self.snap(regime="strong_bear", max_long=6, max_short=2)
        results = [self.fut("LONG").allowed for _ in range(6)]
        self.assertEqual(results, [True, True, True, True, False, False])
        d = self.fut("LONG")
        self.assertIn("Gross futures notional", d.reason)

    def test_pool_uses_frozen_baseline_not_current_equity(self):
        self.snap()
        self.assertTrue(self.spot(equity=100).allowed)  # baseline 100, pool $50, $15 committed
        # Equity doubles but the dollar pool stays anchored to the baseline:
        # 15 + 30 = 45 fits, a further 30 would not (45 + 30 > 50).
        self.assertTrue(self.spot(free=200, equity=200).allowed)  # needs fundable slot 2
        d = self.spot(free=200, equity=200)
        self.assertFalse(d.allowed)  # fundable slot 3 exists but 15+30+30 > 50 pool


# --------------------------------------------------------------------------
# C, D. Standalone modes ignore the Both pools
# --------------------------------------------------------------------------

class StandaloneModeTests(HLBase):
    def test_spot_only_ignores_pools_and_rejects_futures(self):
        self.set_mode("spot_only")
        self.snap(regime="strong_bear", max_spot=8)  # Both spot pool is 0% here
        allowed = [self.spot().allowed for _ in range(7)]
        # $15 each: after five ($75) the 20% live reserve stops the sixth.
        self.assertEqual(allowed, [True] * 5 + [False] * 2)
        d = self.fut("LONG")
        self.assertFalse(d.allowed)
        self.assertIn("spot_only", d.reason)

    def test_spot_only_uses_standalone_max_spot(self):
        self.set_mode("spot_only")
        self.snap(max_spot=2)
        self.assertEqual([self.spot(free=1000, equity=1000).allowed for _ in range(3)], [True, True, False])

    def test_futures_only_uses_market_limits_gross_and_rejects_spot(self):
        self.set_mode("futures_only")
        self.snap(regime="bear", max_long=5, max_short=2)  # Both futures pool 40%: must not apply
        res = [self.fut("LONG" if i < 5 else "SHORT").allowed for i in range(7)]
        # Gross-notional cap (120% of equity, $30 each) stops after four.
        self.assertEqual(res, [True] * 4 + [False] * 3)
        d = self.spot()
        self.assertFalse(d.allowed)
        self.assertIn("futures_only", d.reason)

    def test_futures_only_max_total_and_directions(self):
        self.set_mode("futures_only")
        self.snap(max_long=1, max_short=1)
        self.assertTrue(self.fut("LONG", free=1000, equity=1000).allowed)
        d = self.fut("LONG", free=1000, equity=1000)
        self.assertIn("LONG", d.reason)
        self.assertTrue(self.fut("SHORT", free=1000, equity=1000).allowed)
        self.assertFalse(self.fut("SHORT", free=1000, equity=1000).allowed)


# --------------------------------------------------------------------------
# G. Reserve & accounting
# --------------------------------------------------------------------------

class AccountingTests(HLBase):
    def test_live_reserve_enforced(self):
        self.set_mode("spot_only")
        self.snap()
        d = self.spot(free=34, equity=100)  # 34 - 15 - 0.0225 < 20
        self.assertFalse(d.allowed)
        self.assertIn("minimum reserve", d.reason)
        self.assertTrue(self.spot(free=36, equity=100).allowed)

    def test_reserve_uses_max_of_live_and_baseline(self):
        self.set_mode("spot_only")
        self.snap()
        self.assertTrue(self.spot(free=100, equity=100).allowed)  # baseline = 100
        HLOrderReservation.objects.all().delete()
        # Live equity fell to 50, baseline stays 100 -> floor is still $20.
        d = self.spot(free=30, equity=50)  # 30 - 7.5 - cost = 22.4 >= 20 -> allowed
        self.assertTrue(d.allowed)
        HLOrderReservation.objects.all().delete()
        d = self.spot(free=26, equity=50)
        self.assertFalse(d.allowed)

    def test_fee_and_slippage_buffer_considered(self):
        self.set_mode("spot_only")
        self.snap()
        # Exactly 35.0 would pass without costs (35-15 = 20) but not with them.
        self.assertFalse(self.spot(free=35, equity=100).allowed)

    def test_baseline_frozen_across_heartbeats_and_refreshed_on_regime_change(self):
        self.snap(event_id="H1")
        self.assertTrue(self.spot(equity=100).allowed)
        b = HLEquityBaseline.objects.get(user=self.user)
        self.assertEqual((b.baseline_equity, b.regime_key, b.refresh_reason), (Decimal(100), "strong_bull", "initial"))
        self.snap(event_id="H2", heartbeat_reason="deadman_refresh",
                  bar_close_time_ms=int(time.time() * 1000) - 1000)
        self.spot(free=150, equity=150)
        self.assertEqual(HLEquityBaseline.objects.get(user=self.user).baseline_equity, Decimal(100))
        self.snap(regime="bull", event_id="H3", bar_close_time_ms=int(time.time() * 1000))
        HLOrderReservation.objects.all().delete()
        self.assertTrue(self.spot(free=150, equity=150).allowed)
        b.refresh_from_db()
        self.assertEqual((b.baseline_equity, b.regime_key), (Decimal(150), "bull"))
        self.assertIn("regime_change", b.refresh_reason)

    def test_sizing_uses_entry_equity_not_shrinking_free_balance(self):
        self.set_mode("spot_only")
        self.snap()
        d = self.spot(free=100, equity=200)
        self.assertTrue(d.allowed, d.reason)
        self.assertEqual(d.budget, Decimal(30))  # 15% of equity 200, not of free 100

    def test_exchange_holds_not_double_counted_local_holds_deducted_once(self):
        self.set_mode("spot_only")
        self.snap()
        # Exchange already reflects a $15 hold (free 85): nothing extra deducted locally.
        self.assertTrue(self.spot(free=85, equity=100).allowed)
        # That reservation is PENDING (unreflected): 85 - 15 local hold - 15 - cost >= 20 -> ok.
        self.assertTrue(self.spot(free=85, equity=100).allowed)
        # Third: 85 - 30 - 15 - cost = 39.9 -> ok; fourth: 85 - 45 - 15 = 24.9 ok; fifth fails.
        self.assertTrue(self.spot(free=85, equity=100).allowed)
        self.assertTrue(self.spot(free=85, equity=100).allowed)
        self.assertFalse(self.spot(free=85, equity=100).allowed)

    def test_confirmed_reservation_stops_counting(self):
        self.set_mode("spot_only")
        self.snap(max_spot=1)
        d = self.spot()
        self.assertTrue(d.allowed)
        self.assertFalse(self.spot().allowed)  # slot held by the pending reservation
        d.mark_submitting()
        d.confirm("ORD1")
        so(self.user, order_id="ORD1", order_quantity=Decimal("0.15"), final_quantity=Decimal("0.15"),
           entry_price=Decimal(100))
        self.assertEqual(market_risk._counts(self.user)[0], 1)  # row counted once, not twice
        self.assertFalse(self.spot().allowed)

    def test_missing_exchange_values_reject(self):
        self.snap()
        self.assertFalse(size_entry(self.user, "spot", None, None, "S", equity_usdc=100).allowed)
        self.assertFalse(size_entry(self.user, "spot", 100, None, "S", equity_usdc=None).allowed)
        self.assertFalse(size_entry(self.user, "spot", 100, None, "S", equity_usdc=0).allowed)

    def test_stop_risk_requires_valid_stop_and_caps_aggregate(self):
        self.set_mode("futures_only")
        self.snap(max_long=6, max_short=1)
        d = size_entry(self.user, "futures", 100, "LONG", "S", equity_usdc=100, entry_price=100, sl_price=None)
        self.assertFalse(d.allowed)
        self.assertIn("stop", d.reason)
        d = self.fut("LONG", sl=101)  # wrong side
        self.assertFalse(d.allowed)
        # 30 notional with a 10% stop = $3 + costs each: second breaches 4% of equity.
        self.assertTrue(self.fut("LONG", sl=90).allowed)
        d = self.fut("LONG", sl=90)
        self.assertFalse(d.allowed)
        self.assertIn("Aggregate stop risk", d.reason)

    def test_kill_switch_blocks_everything(self):
        self.snap()
        with override_settings(HL_KILL_SWITCH=True):
            self.assertIn("kill switch", self.spot().reason)
            self.assertFalse(can_open_spot_position(self.user, "A", exchange=HL)[0])


# --------------------------------------------------------------------------
# Reservation lifecycle (G, concurrency safety)
# --------------------------------------------------------------------------

class ReservationLifecycleTests(HLBase):
    def setUp(self):
        super().setUp()
        self.set_mode("spot_only")
        self.snap(max_spot=1)

    def run_scope(self, exc=None, submit=True):
        d = self.spot()
        self.assertTrue(d.allowed)
        scope = reservation_scope(d)
        with scope:
            if submit:
                d.mark_submitting()
            if exc:
                raise exc
        d.reservation.refresh_from_db()
        return d, scope

    def test_failure_before_submit_releases(self):
        d, scope = self.run_scope(exc=ValueError("below min"), submit=False)
        self.assertEqual(d.reservation.status, S.RELEASED)
        self.assertIsNotNone(scope.error)
        self.assertTrue(self.spot().allowed)

    def test_clean_exchange_rejection_releases(self):
        d, _ = self.run_scope(exc=ccxt.InsufficientFunds("no funds"))
        self.assertEqual(d.reservation.status, S.RELEASED)

    def test_timeout_keeps_capacity_held_as_uncertain(self):
        d, _ = self.run_scope(exc=ccxt.RequestTimeout("timeout"))
        self.assertEqual(d.reservation.status, S.UNCERTAIN)
        self.assertFalse(self.spot().allowed)  # slot NOT released on a guess

    def test_unconfirmed_submission_without_error_is_uncertain(self):
        d, _ = self.run_scope()
        self.assertEqual(d.reservation.status, S.UNCERTAIN)

    def test_stale_pending_released_but_uncertain_never_auto_released(self):
        d = self.spot()
        HLOrderReservation.objects.filter(pk=d.reservation.pk).update(
            created_at=d.reservation.created_at - market_risk.timedelta(hours=1))
        self.assertEqual(market_risk.release_stale_pending_reservations(self.user), 1)
        HLOrderReservation.objects.all().delete()
        u = self.spot()
        HLOrderReservation.objects.filter(pk=u.reservation.pk).update(
            status=S.UNCERTAIN, created_at=u.reservation.created_at - market_risk.timedelta(hours=9))
        self.assertEqual(market_risk.release_stale_pending_reservations(self.user), 0)


# --------------------------------------------------------------------------
# F. Concurrency
# --------------------------------------------------------------------------

class ConcurrentFuturesTests(TransactionTestCase):
    def test_concurrent_long_and_short_cannot_oversubscribe(self):
        user = User.objects.create_user("conc", password="x")
        TradeSettings.get_for_user(user)
        add_hl_key(user, "futures_only")
        market_risk.ingest_payload(make_payload(regime="strong_bear", max_long=6, max_short=2))
        results = []

        def worker(direction):
            try:
                with user_trade_open_lock(user.id):
                    d = size_entry(user, "futures", 100, direction, "S", equity_usdc=100,
                                   entry_price=100, sl_price=97 if direction == "LONG" else 103)
                    results.append(d.allowed)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=("LONG" if i % 2 == 0 else "SHORT",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 120% gross cap / $30 notional => exactly four full-size entries.
        self.assertEqual(sum(results), 4)
        self.assertEqual(HLOrderReservation.objects.filter(status=S.PENDING).count(), 4)


# --------------------------------------------------------------------------
# I. Regime transitions / existing positions
# --------------------------------------------------------------------------

class RegimeTransitionTests(HLBase):
    def test_downgrade_tightens_new_entries_but_keeps_positions(self):
        self.snap(event_id="BULL", regime="strong_bull")
        for _ in range(3):
            row = so(self.user, order_quantity=Decimal("0.15"), final_quantity=Decimal("0.15"), entry_price=Decimal(100))
        self.assertEqual(SpotOrder.objects.filter(user=self.user, status="POSITION").count(), 3)
        self.snap(event_id="BEAR", regime="bear", bar_close_time_ms=int(time.time() * 1000))
        d = self.spot()
        self.assertFalse(d.allowed)  # Bear spot pool is 0%
        # Existing rows untouched (no forced close) and still count.
        self.assertEqual(SpotOrder.objects.filter(user=self.user, status="POSITION").count(), 3)
        self.assertEqual(market_risk._counts(self.user)[0], 3)

    def test_upgrade_does_not_open_anything_by_itself(self):
        self.snap(event_id="BEAR", regime="bear")
        self.snap(event_id="SB", regime="strong_bull", bar_close_time_ms=int(time.time() * 1000))
        self.assertEqual(SpotOrder.objects.count() + FutureOrder.objects.count(), 0)
        self.assertEqual(HLOrderReservation.objects.count(), 0)

    def test_capacity_only_released_after_exchange_reconciled_status(self):
        self.snap(max_spot=1)
        self.set_mode("spot_only")
        row = so(self.user, order_quantity=Decimal("0.15"), final_quantity=Decimal("0.15"),
                 entry_price=Decimal(100), status="OPEN")
        self.assertFalse(self.spot().allowed)  # resting order holds the slot
        SpotOrder.objects.filter(pk=row.pk).update(status="CANCELLED")  # set by refresh job after exchange confirms
        self.assertTrue(self.spot().allowed)

    def test_partial_fill_counts_filled_quantity(self):
        self.set_mode("spot_only")
        self.snap(max_spot=2)
        so(self.user, order_quantity=Decimal("0.15"), final_quantity=Decimal("0.05"),
           entry_price=Decimal(100))
        self.assertEqual(market_risk._spot_committed(self.user), Decimal(5))


# --------------------------------------------------------------------------
# Gate basics (mode, freshness, risk_guard routing, Binance regression)
# --------------------------------------------------------------------------

class GateTests(HLBase):
    def test_no_snapshot_blocks_but_binance_unaffected(self):
        ok, reason = can_open_futures_position(self.user, "BTCUSDC", "LONG", exchange=HL)
        self.assertFalse(ok)
        self.assertIn("No market-risk snapshot", reason)
        self.ts.last_market_risk_update = None
        self.ts.sync_risk_from_webhook = False
        self.ts.save()
        self.assertTrue(can_open_futures_position(self.user, "BTCUSDT", "LONG")[0])
        self.assertTrue(can_open_spot_position(self.user, "BTCUSDT")[0])

    def test_stale_snapshot_blocks_independently_of_tradingview(self):
        s = self.snap()
        MarketRiskSnapshot.objects.filter(pk=s.pk).update(event_time_ms=int(time.time() * 1000) - 6000 * 1000)
        self.assertFalse(market_risk.snapshot_is_fresh(market_risk.get_current_snapshot()))
        ok, reason = can_open_spot_position(self.user, "HYPE", exchange=HL)
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_default_stale_timeout_is_90_minutes(self):
        p = make_payload()
        del p["stale_after_sec"]
        self.assertEqual(market_risk.ingest_payload(p).stale_after_sec, 5400)

    def test_mode_controls_markets(self):
        self.snap()
        self.set_mode("spot_only")
        self.assertTrue(can_open_spot_position(self.user, "HYPE", exchange=HL)[0])
        self.assertIn("spot_only", can_open_futures_position(self.user, "ETH", "LONG", exchange=HL)[1])
        self.set_mode("futures_only")
        self.assertTrue(can_open_futures_position(self.user, "ETH", "LONG", exchange=HL)[0])
        self.assertFalse(can_open_spot_position(self.user, "HYPE", exchange=HL)[0])
        self.set_mode("both")
        self.assertTrue(can_open_spot_position(self.user, "HYPE", exchange=HL)[0])
        self.assertTrue(can_open_futures_position(self.user, "ETH", "SHORT", exchange=HL)[0])

    def test_one_position_per_symbol_still_enforced(self):
        self.snap()
        fo(self.user, symbol="ETH", direction="LONG", order_quantity=Decimal(1), entry_price=Decimal(10))
        self.assertFalse(can_open_futures_position(self.user, "ETH", "LONG", exchange=HL)[0])

    def test_manual_caps_when_auto_update_off_but_never_above_hard_limit(self):
        self.snap()
        TradeSettings.objects.filter(user=self.user).update(
            hl_sync_from_webhook=False, hl_futures_max_long=1, hl_futures_max_short=1, hl_futures_max_positions=1)
        fo(self.user, direction="LONG", order_quantity=Decimal(1), entry_price=Decimal(10))
        self.assertFalse(can_open_futures_position(self.user, "NEW", "LONG", exchange=HL)[0])

    def test_trading_mode_change_syncs_settings(self):
        self.snap()
        self.set_mode("futures_only")
        market_risk.sync_settings_to_mode(self.user)
        ts = TradeSettings.objects.get(user=self.user)
        self.assertEqual((ts.hl_futures_max_long, ts.hl_futures_max_short, ts.hl_futures_max_positions), (5, 2, 7))
        self.assertEqual(ts.hl_futures_leverage, 3)

    def test_decisions_audited(self):
        self.snap()
        self.set_mode("spot_only")
        self.fut("LONG")
        self.assertTrue(RiskDecisionLog.objects.filter(user=self.user, allowed=False, market="futures").exists())

    def test_binance_risk_guard_unchanged(self):
        """Binance limits ignore HL rows and the HL controller entirely."""
        TradeSettings.objects.filter(user=self.user).update(
            sync_risk_from_webhook=False, futures_max_positions=1, futures_max_long=1, futures_max_short=1)
        fo(self.user, exchange="BINANCE_FUTURES", direction="LONG", order_quantity=Decimal(1), entry_price=Decimal(10))
        ok, reason = can_open_futures_position(self.user, "SOLUSDT", "LONG", exchange="BINANCE_FUTURES")
        self.assertFalse(ok)
        self.assertIn("Max futures positions", reason)
        # No HL snapshot exists, yet a Binance entry on an empty book is allowed.
        FutureOrder.objects.all().delete()
        self.assertTrue(can_open_futures_position(self.user, "SOLUSDT", "LONG", exchange="BINANCE_FUTURES")[0])
