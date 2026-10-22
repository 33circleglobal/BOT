import copy
import json
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from apps.accounts.models import UserHyperLiquidKey
from apps.trade.models import (
    FutureOrder,
    MarketRiskSnapshot,
    RiskDecisionLog,
    SpotOrder,
    TradeSettings,
    WebhookLog,
)
from apps.trade.utils import market_risk
from apps.trade.utils.risk_guard import can_open_futures_position, can_open_spot_position

User = get_user_model()
HL = "HYPERLIQUID"
SECRET = "s3cret"


def make_payload(**over):
    now = int(time.time() * 1000)
    p = {
        "action": "update_market_risk",
        "schema_version": 5,
        "release_version": "V3.2",
        "controller_version": "V3.2",
        "exchange": "HyperLiquid",
        "account_mode": "unified",
        "collateral_asset": "USDC",
        "strategy": "122Moon",
        "event_type": "heartbeat",
        "event_id": f"EVT-{now}",
        "event_time_ms": now,
        "primary_state_time_ms": now - 600000,
        "stale_after_sec": 5400,
        "member_mode_selection": "backend_per_member",
        "supported_member_modes": ["spot_only", "futures_only", "both"],
        "current_market_regime": {"source": "alt_market", "key": "strong_bull", "label": "Strong Bull", "value": 2},
        "data_health": {"alt_ok": True, "btc_ok": True},
        "allocation_profiles": {
            "spot_only": {"spot_pct": 85, "futures_pct": 0, "reserve_pct": 15, "target_spot_pct": 90,
                          "max_spot": 10, "max_long": 0, "max_short": 0, "max_total": 0},
            "futures_only": {"spot_pct": 0, "futures_pct": 85, "reserve_pct": 15, "target_futures_pct": 85,
                             "max_spot": 0, "max_long": 7, "max_short": 3, "max_total": 10},
            "both": {"spot_pct": 55, "futures_pct": 30, "reserve_pct": 15, "target_spot_pct": 55,
                     "target_futures_pct": 30, "max_spot": 10, "max_long": 7, "max_short": 3, "max_total": 10},
        },
        # Legacy Both-controller blocks: must never override a single-market profile.
        "spot": {"allocation_pct": 55, "trade_size_pct_of_spot_pool": 15, "max_trades": 10},
        "futures": {"margin_mode": "isolated", "leverage": 3, "allocation_pct": 30,
                    "margin_per_trade_pct_of_futures_pool": 10, "max_long": 7, "max_short": 3, "max_total": 10},
    }
    p.update(over)
    return p


_n = [0]


def fo(**kw):
    _n[0] += 1
    kw.setdefault("stop_loss_order_id", f"SL-{_n[0]}")
    return FutureOrder.objects.create(**kw)


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
        self.assertEqual(snap.allocation_profiles["both"]["spot_pct"], "55")
        self.assertEqual(snap.raw_payload["event_id"], p["event_id"])
        self.assertEqual(WebhookLog.objects.get().event_id, p["event_id"])

    def test_missing_wrong_or_unconfigured_secret_rejected(self):
        self.assertEqual(post(self.client, make_payload(), secret="").status_code, 401)
        self.assertEqual(post(self.client, make_payload(), secret="nope").status_code, 401)
        self.assertEqual(MarketRiskSnapshot.objects.count(), 0)
        with override_settings(TRADINGVIEW_WEBHOOK_SECRET=""):
            self.assertEqual(post(self.client, make_payload(), secret="").status_code, 401)

    def test_header_secret_accepted(self):
        r = self.client.post("/webhook/", data=json.dumps(make_payload()),
                             content_type="application/json", HTTP_X_WEBHOOK_SECRET=SECRET)
        self.assertEqual(r.status_code, 200)

    def test_invalid_identity_fields(self):
        self.reject(make_payload(schema_version=4))
        self.reject(make_payload(schema_version="five"))
        self.reject(make_payload(exchange="Binance", schema_version=5))
        self.reject(make_payload(account_mode="cross"))
        self.reject(make_payload(collateral_asset="USDT"))
        self.reject(make_payload(member_mode_selection="tradingview"))

    def test_missing_required_fields(self):
        for key in ("event_id", "event_time_ms", "primary_state_time_ms", "stale_after_sec",
                    "allocation_profiles", "data_health", "current_market_regime"):
            p = make_payload()
            del p[key]
            self.reject(p)

    def test_missing_or_incomplete_profile(self):
        p = make_payload()
        del p["allocation_profiles"]["both"]
        self.reject(p)
        p = make_payload()
        del p["allocation_profiles"]["spot_only"]["max_spot"]
        self.reject(p)

    def test_invalid_numbers_and_profile_coherence(self):
        for mutate in (
            lambda p: p["allocation_profiles"]["both"].update(spot_pct=120),
            lambda p: p["allocation_profiles"]["both"].update(spot_pct="abc"),
            lambda p: p["allocation_profiles"]["both"].update(spot_pct=-5),
            lambda p: p["allocation_profiles"]["both"].update(reserve_pct=50),  # sum != 100
            lambda p: p["allocation_profiles"]["both"].update(max_total=9),  # != long+short
            lambda p: p["allocation_profiles"]["spot_only"].update(futures_pct=5, reserve_pct=10),
            lambda p: p["allocation_profiles"]["futures_only"].update(max_spot=2),
            lambda p: p.update(stale_after_sec=0),
            lambda p: p.update(stale_after_sec=10**9),
            lambda p: p["futures"].update(leverage=50),
            lambda p: p["futures"].update(margin_mode="cross"),
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

    def test_older_state_cannot_overwrite_newer(self):
        new = make_payload(event_id="NEW")
        post(self.client, new)
        old = make_payload(event_id="OLD")
        old["primary_state_time_ms"] = new["primary_state_time_ms"] - 1000
        old["event_time_ms"] = new["event_time_ms"] + 1  # later send, older state
        r = post(self.client, old)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(market_risk.get_current_snapshot().event_id, "NEW")

    def test_same_state_older_event_rejected_newer_accepted(self):
        a = make_payload(event_id="A")
        post(self.client, a)
        b = make_payload(event_id="B", primary_state_time_ms=a["primary_state_time_ms"],
                         event_time_ms=a["event_time_ms"] - 5)
        self.assertEqual(post(self.client, b).status_code, 400)
        c = make_payload(event_id="C", primary_state_time_ms=a["primary_state_time_ms"],
                         event_time_ms=a["event_time_ms"] + 5)
        self.assertEqual(post(self.client, c).status_code, 200)
        self.assertEqual(market_risk.get_current_snapshot().event_id, "C")

    def test_stale_or_future_event_rejected(self):
        now = int(time.time() * 1000)
        self.reject(make_payload(event_time_ms=now - 6000 * 1000, primary_state_time_ms=now - 6100 * 1000))
        self.reject(make_payload(event_time_ms=now + 3600 * 1000))

    def test_alt_feed_down_requires_conservative_profile(self):
        # Controller sending bull allocations while claiming ALT is down is incoherent.
        self.reject(make_payload(data_health={"alt_ok": False, "btc_ok": True}))
        p = make_payload(data_health={"alt_ok": False, "btc_ok": True})
        p["allocation_profiles"] = {
            "spot_only": {"spot_pct": 0, "futures_pct": 0, "reserve_pct": 100, "max_spot": 0,
                          "max_long": 0, "max_short": 0, "max_total": 0},
            "futures_only": {"spot_pct": 0, "futures_pct": 20, "reserve_pct": 80, "max_spot": 0,
                             "max_long": 1, "max_short": 1, "max_total": 2},
            "both": {"spot_pct": 0, "futures_pct": 20, "reserve_pct": 80, "max_spot": 0,
                     "max_long": 1, "max_short": 1, "max_total": 2},
        }
        self.assertEqual(post(self.client, p).status_code, 200)

    def test_unhealthy_data_health_shape_rejected(self):
        self.reject(make_payload(data_health={"btc_ok": True}))
        self.reject(make_payload(data_health="ok"))

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

    def test_v5_update_does_not_touch_trade_settings_caps(self):
        u = User.objects.create_user("capuser", password="x")
        before = TradeSettings.get_for_user(u).futures_max_long
        post(self.client, make_payload())
        self.assertEqual(TradeSettings.objects.get(user=u).futures_max_long, before)


class GateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("hl", password="x")
        self.ts = TradeSettings.get_for_user(self.user)
        self.key = add_hl_key(self.user)

    def snap(self, **over):
        p = make_payload(**over)
        return market_risk.ingest_payload(p)

    def set_mode(self, mode):
        UserHyperLiquidKey.objects.filter(pk=self.key.pk).update(trading_mode=mode)

    def test_no_snapshot_blocks_but_binance_unaffected(self):
        ok, reason = can_open_futures_position(self.user, "BTCUSDC", "LONG", exchange=HL)
        self.assertFalse(ok)
        self.assertIn("No market-risk snapshot", reason)
        self.ts.last_market_risk_update = None
        self.ts.sync_risk_from_webhook = False
        self.ts.save()
        self.assertTrue(can_open_futures_position(self.user, "BTCUSDT", "LONG")[0])
        self.assertTrue(can_open_spot_position(self.user, "BTCUSDT")[0])

    def test_stale_snapshot_blocks(self):
        s = self.snap()
        MarketRiskSnapshot.objects.filter(pk=s.pk).update(
            event_time_ms=int(time.time() * 1000) - 6000 * 1000)
        self.assertFalse(market_risk.snapshot_is_fresh(market_risk.get_current_snapshot()))
        ok, reason = can_open_spot_position(self.user, "HYPE", exchange=HL)
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_mode_controls_markets(self):
        self.snap()
        self.set_mode("spot_only")
        self.assertTrue(can_open_spot_position(self.user, "HYPE", exchange=HL)[0])
        ok, reason = can_open_futures_position(self.user, "ETH", "LONG", exchange=HL)
        self.assertFalse(ok)
        self.assertIn("spot_only", reason)
        self.set_mode("futures_only")
        self.assertTrue(can_open_futures_position(self.user, "ETH", "LONG", exchange=HL)[0])
        self.assertFalse(can_open_spot_position(self.user, "HYPE", exchange=HL)[0])
        self.set_mode("both")
        self.assertTrue(can_open_spot_position(self.user, "HYPE", exchange=HL)[0])
        self.assertTrue(can_open_futures_position(self.user, "ETH", "SHORT", exchange=HL)[0])

    def test_zero_allocation_blocks(self):
        p = make_payload()
        p["allocation_profiles"]["both"].update(spot_pct=0, reserve_pct=70)
        market_risk.ingest_payload(p)
        d = market_risk.evaluate_entry(self.user, "spot")
        self.assertFalse(d.allowed)
        self.assertIn("0%", d.reason)

    def test_legacy_blocks_cannot_override_single_market_profile(self):
        p = make_payload()
        p["spot"]["allocation_pct"] = 99
        p["capital_allocation"] = {"spot_pct": 99, "futures_pct": 99}
        market_risk.ingest_payload(p)
        self.set_mode("futures_only")
        self.assertFalse(market_risk.evaluate_entry(self.user, "spot").allowed)
        d = market_risk.evaluate_entry(self.user, "futures", "LONG")
        self.assertEqual(d.effective_pct, Decimal(85))

    def test_futures_caps_long_short_total(self):
        p = make_payload()
        p["allocation_profiles"]["both"].update(max_long=2, max_short=1, max_total=3)
        market_risk.ingest_payload(p)
        for i, direction in enumerate(["LONG", "LONG"]):
            fo(order_id=f"o{i}", symbol=f"S{i}", direction=direction,
                                       status="POSITION", exchange=HL, user=self.user, leverage=3)
        self.assertFalse(can_open_futures_position(self.user, "NEW", "LONG", exchange=HL)[0])
        self.assertTrue(can_open_futures_position(self.user, "NEW", "SHORT", exchange=HL)[0])
        fo(order_id="o9", symbol="SH", direction="SHORT",
                                   status="OPEN", exchange=HL, user=self.user, leverage=3)  # pending counts
        ok, reason = can_open_futures_position(self.user, "NEW", "SHORT", exchange=HL)
        self.assertFalse(ok)
        self.assertIn("Max", reason)

    def test_max_total_enforced_independently(self):
        p = make_payload()
        p["allocation_profiles"]["both"].update(max_long=7, max_short=3, max_total=10)
        market_risk.ingest_payload(p)
        with self.settings(HL_HARD_MAX_FUTURES_POSITIONS=2):
            for i in range(2):
                fo(order_id=f"t{i}", symbol=f"T{i}", direction="LONG",
                                           status="POSITION", exchange=HL, user=self.user, leverage=3)
            ok, _ = can_open_futures_position(self.user, "X", "SHORT", exchange=HL)
            self.assertFalse(ok)  # webhook allows 10, hard limit 2

    def test_spot_cap_and_hard_limit(self):
        self.snap()
        with self.settings(HL_HARD_MAX_SPOT_POSITIONS=1):
            self.assertTrue(can_open_spot_position(self.user, "A", exchange=HL)[0])
            SpotOrder.objects.create(order_id="s1", symbol="A", status="POSITION", exchange=HL,
                                     user=self.user, entry_price=1, order_quantity=1)
            self.assertFalse(can_open_spot_position(self.user, "B", exchange=HL)[0])

    def test_binance_positions_do_not_count_against_hyperliquid(self):
        self.snap()
        fo(order_id="b1", symbol="BTCUSDT", direction="LONG", status="POSITION",
                                   exchange="BINANCE_FUTURES", user=self.user, leverage=5)
        d = market_risk.evaluate_entry(self.user, "futures", "LONG")
        self.assertTrue(d.allowed)

    def test_manual_caps_when_auto_update_off_still_need_fresh_snapshot(self):
        self.ts.hl_sync_from_webhook = False
        self.ts.hl_futures_max_long = 0
        self.ts.save()
        self.snap()
        self.assertFalse(market_risk.evaluate_entry(self.user, "futures", "LONG").allowed)
        MarketRiskSnapshot.objects.all().delete()
        self.assertFalse(market_risk.evaluate_entry(self.user, "futures", "SHORT").allowed)

    def test_binance_settings_do_not_affect_hyperliquid(self):
        self.snap()
        self.ts.futures_max_long = 0
        self.ts.spot_max_positions = 0
        self.ts.futures_leverage = 1
        self.ts.save()
        self.assertTrue(market_risk.evaluate_entry(self.user, "futures", "LONG").allowed)
        self.assertTrue(market_risk.evaluate_entry(self.user, "spot").allowed)
        self.assertEqual(market_risk.evaluate_entry(self.user, "futures", "LONG").leverage, 3)

    def test_manual_position_size_used_only_when_auto_update_off(self):
        self.snap()
        self.ts.hl_spot_trade_pct = Decimal("5")
        self.ts.hl_futures_margin_pct = Decimal("2")
        self.ts.save()
        self.assertEqual(market_risk.size_entry(self.user, "spot", 1000).budget, Decimal("82.5"))  # controller 15% of 550
        self.ts.hl_sync_from_webhook = False
        self.ts.save()
        self.assertEqual(market_risk.size_entry(self.user, "spot", 1000).budget, Decimal("27.5"))  # 5% of 550
        self.assertEqual(market_risk.size_entry(self.user, "futures", 1000, "LONG").budget, Decimal("6"))  # 2% of 300

    def test_rejections_are_audited(self):
        can_open_spot_position(self.user, "HYPE", exchange=HL)
        self.assertTrue(RiskDecisionLog.objects.filter(user=self.user, allowed=False).exists())


class SizingTests(TestCase):
    """Guide section 5 / 12 examples with $1,000 equity."""

    def setUp(self):
        self.user = User.objects.create_user("size", password="x")
        TradeSettings.get_for_user(self.user)
        add_hl_key(self.user)
        market_risk.ingest_payload(make_payload())

    def mode(self, m):
        UserHyperLiquidKey.objects.filter(user=self.user).update(trading_mode=m)

    def test_spot_only(self):
        self.mode("spot_only")
        d = market_risk.size_entry(self.user, "spot", 1000)
        self.assertEqual(d.budget, Decimal("127.5"))

    def test_futures_only_margin_and_notional(self):
        self.mode("futures_only")
        d = market_risk.size_entry(self.user, "futures", 1000, "LONG")
        self.assertEqual(d.budget, Decimal("85"))
        self.assertEqual(d.leverage, 3)
        self.assertEqual(d.budget * d.leverage, Decimal("255"))

    def test_both(self):
        self.mode("both")
        self.assertEqual(market_risk.size_entry(self.user, "spot", 1000).budget, Decimal("82.5"))
        d = market_risk.size_entry(self.user, "futures", 1000, "LONG")
        self.assertEqual(d.budget, Decimal("30"))
        self.assertEqual(d.budget * d.leverage, Decimal("90"))

    def test_effective_not_target_is_used(self):
        # spot_only profile: effective 85, target 90 in make_payload.
        self.mode("spot_only")
        self.assertEqual(market_risk.size_entry(self.user, "spot", 1000).effective_pct, Decimal(85))

    def test_member_leverage_ceiling(self):
        self.mode("futures_only")
        TradeSettings.objects.filter(user=self.user).update(hl_futures_leverage=2)
        self.assertEqual(market_risk.size_entry(self.user, "futures", 1000, "LONG").leverage, 2)

    def test_pool_exhaustion_and_commitments_reduce_budget(self):
        self.mode("both")
        # Futures pool 30% of equity; commit nearly all of it already.
        fo(order_id="c1", symbol="E", direction="LONG", status="POSITION",
                                   exchange=HL, user=self.user, leverage=3,
                                   order_quantity=Decimal("1"), entry_price=Decimal("880"))  # margin ~293.33
        # equity = free 706.67 + 293.33 = 1000 -> pool 300, left ~6.67
        d = market_risk.size_entry(self.user, "futures", Decimal("706.67"), "SHORT")
        self.assertTrue(d.allowed)
        self.assertLess(d.budget, Decimal("7"))
        FutureOrder.objects.filter(order_id="c1").update(entry_price=Decimal("900"))  # margin 300 -> full
        d = market_risk.size_entry(self.user, "futures", Decimal("700"), "SHORT")
        self.assertFalse(d.allowed)

    def test_insufficient_collateral(self):
        self.mode("both")
        d = market_risk.size_entry(self.user, "spot", 0)
        self.assertFalse(d.allowed)

    def test_budget_never_exceeds_free_collateral(self):
        self.mode("spot_only")
        SpotOrder.objects.create(order_id="sp", symbol="Z", status="POSITION", exchange=HL, user=self.user,
                                 entry_price=Decimal("100"), order_quantity=Decimal("0.1"), final_quantity=Decimal("0.1"))
        d = market_risk.size_entry(self.user, "spot", Decimal("8"))  # equity 18, pool 15.3
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.budget, Decimal("8"))
        # Committed spot already above the pool -> blocked, not clipped.
        SpotOrder.objects.filter(order_id="sp").update(order_quantity=5, final_quantity=5)
        self.assertFalse(market_risk.size_entry(self.user, "spot", Decimal("3")).allowed)

    def test_reserve_is_protected_across_markets(self):
        self.mode("both")  # reserve 15%
        SpotOrder.objects.create(order_id="sp2", symbol="Z", status="POSITION", exchange=HL, user=self.user,
                                 entry_price=Decimal("100"), order_quantity=Decimal("5.4"), final_quantity=Decimal("5.4"))
        fo(order_id="f2", symbol="E", direction="LONG", status="POSITION",
                                   exchange=HL, user=self.user, leverage=3,
                                   order_quantity=Decimal("1"), entry_price=Decimal("900"))
        # committed 540 + 300 = 840; equity = 160 + 840 = 1000; max committed 850 -> 10 left
        d = market_risk.size_entry(self.user, "spot", Decimal("160"))
        self.assertLessEqual(d.budget or 0, Decimal("10"))


class ModeTransitionTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("mt", password="x")
        TradeSettings.get_for_user(self.u)
        self.key = add_hl_key(self.u, "spot_only")

    def test_cancels_only_unfilled_entries_in_disabled_market(self):
        from apps.trade.task import cancel_disabled_market_pending_entries
        u = self.u
        fo(order_id="p1", symbol="E", direction="LONG", status="OPEN", exchange=HL, user=u, leverage=3)
        fo(order_id="p2", symbol="F", direction="LONG", status="POSITION", exchange=HL, user=u, leverage=3)
        with patch("apps.trade.task.cancel_pending_hyperliquid_limit_order") as cancel, \
                patch("apps.trade.task.cancel_pending_hyperliquid_spot_limit_order") as cancel_spot:
            cancel_disabled_market_pending_entries.apply(args=[u.id])
            cancel_disabled_market_pending_entries.apply(args=[u.id])  # retry-safe
        self.assertEqual([c.args[0].order_id for c in cancel.call_args_list], ["p1", "p1"])
        cancel_spot.assert_not_called()

    def test_no_key_blocks_hyperliquid_entries(self):
        u2 = User.objects.create_user("nokey", email="nokey@x.com", password="x")
        market_risk.ingest_payload(make_payload())
        d = market_risk.evaluate_entry(u2, "spot")
        self.assertFalse(d.allowed)
        self.assertIn("API key", d.reason)


@override_settings(TRADINGVIEW_WEBHOOK_SECRET=SECRET)
class ApiKeyModeFlowTests(TestCase):
    """Mode is set on the HyperLiquid API key; Risk Settings follow it."""

    def setUp(self):
        self.u = User.objects.create_user("flow", password="x")
        self.key = add_hl_key(self.u, "both")
        self.c = Client()
        self.c.force_login(self.u)
        p = make_payload()
        p["allocation_profiles"]["futures_only"].update(max_long=5, max_short=2, max_total=7)
        market_risk.ingest_payload(p)

    @patch("apps.trade.task.cancel_disabled_market_pending_entries.delay")
    def test_mode_change_syncs_risk_settings_and_cancels(self, cancel):
        r = self.c.post("/profile/", {"action": "save_hl_mode", "trading_mode": "futures_only"})
        self.assertEqual(r.status_code, 302)
        self.key.refresh_from_db()
        self.assertEqual(self.key.trading_mode, "futures_only")
        ts = TradeSettings.objects.get(user=self.u)
        self.assertEqual((ts.hl_futures_max_long, ts.hl_futures_max_short, ts.hl_futures_max_positions, ts.hl_spot_max_positions),
                         (5, 2, 7, 0))
        cancel.assert_called_once_with(self.u.id)
        # the gate now follows the new mode
        self.assertFalse(market_risk.evaluate_entry(self.u, "spot").allowed)

    @patch("apps.trade.task.cancel_disabled_market_pending_entries.delay")
    def test_mode_change_allowed_with_open_positions(self, cancel):
        fo(order_id="x1", symbol="E", direction="LONG", status="POSITION", exchange=HL, user=self.u, leverage=3)
        self.c.post("/profile/", {"action": "save_hl_mode", "trading_mode": "spot_only"})
        self.key.refresh_from_db()
        self.assertEqual(self.key.trading_mode, "spot_only")
        self.assertTrue(FutureOrder.objects.filter(order_id="x1", status="POSITION").exists())

    def test_invalid_mode_rejected(self):
        self.c.post("/profile/", {"action": "save_hl_mode", "trading_mode": "yolo"})
        self.key.refresh_from_db()
        self.assertEqual(self.key.trading_mode, "both")

    def test_risk_settings_page_shows_mode_readonly(self):
        r = self.c.get("/risk-settings/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "API key settings")
        self.assertNotIn("hl_trading_mode", r.content.decode())
