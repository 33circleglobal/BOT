from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.trade.models import FutureOrder, SpotOrder, SpotTakeProfit, TradeSettings
from apps.trade.utils.create_market_binance_spot_order import (
    create_binance_spot_order,
    activate_filled_spot_limit_order,
)
from apps.trade.utils.create_market_hyperliquid_order import (
    create_hyperliquid_future_order,
    activate_filled_hyperliquid_limit_order,
)
from apps.trade.utils.risk_guard import can_open_spot_position
from apps.trade.utils import market_risk
from apps.trade.test_market_risk import make_payload, add_hl_key

User = get_user_model()


def _exchange():
    ex = MagicMock()
    ex.amountToPrecision.side_effect = lambda s, a: str(round(float(a), 4))
    ex.priceToPrecision.side_effect = lambda s, p: str(round(float(p), 4))
    ex.market.return_value = {
        "base": "ABC",
        "quote": "USDT",
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 1}},
    }
    return ex


class SpotLimitOrderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        TradeSettings.get_for_user(self.user)

    @patch("apps.trade.utils.create_market_binance_spot_order.can_open_spot_position", return_value=(True, ""))
    @patch("apps.trade.utils.create_market_binance_spot_order.get_symbol_last_price", return_value=110.0)
    @patch("apps.trade.utils.create_market_binance_spot_order.make_spot_exchange")
    @patch("apps.trade.utils.create_market_binance_spot_order.UserKey")
    def test_limit_buy_rests_then_activates(self, _key, make_ex, _price, _allowed):
        ex = _exchange()
        ex.fetch_balance.return_value = {"free": {"USDT": 1000.0}}
        ex.create_order.return_value = {"id": "L1"}
        make_ex.return_value = ex

        ok = create_binance_spot_order(
            "buy", "ABCUSDT", self.user, tps=[{"price": 130, "percent": 100}], sl=90,
            order_type="limit", entry=100,
        )
        self.assertTrue(ok)
        o = SpotOrder.objects.get(order_id="L1")
        self.assertEqual(o.status, "OPEN")
        self.assertEqual(float(o.entry_price), 100.0)
        self.assertEqual(o.pending_tps, [{"price": 130.0, "percent": 100.0}])
        self.assertEqual(ex.create_order.call_args.kwargs["type"], "limit")
        self.assertFalse(can_open_spot_position(self.user, "ABCUSDT")[0])  # OPEN counts

        ex.create_order.reset_mock()
        ex.create_order.return_value = {"id": "TP1"}
        activate_filled_spot_limit_order(
            ex, o, {"filled": 1.0, "average": 99.0, "fees": [{"cost": 0.001, "currency": "ABC"}]}
        )
        o.refresh_from_db()
        self.assertEqual(o.status, "POSITION")
        self.assertAlmostEqual(float(o.final_quantity), 0.999)
        self.assertEqual(SpotTakeProfit.objects.filter(order=o).count(), 1)


class HyperliquidFuturesLimitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="h", password="x")
        TradeSettings.get_for_user(self.user)
        # HyperLiquid entries are gated/sized by the 122Moon snapshot.
        add_hl_key(self.user)
        market_risk.ingest_payload(make_payload())

    @patch("apps.trade.utils.create_market_hyperliquid_order.create_trigger_order", return_value={"id": "T1"})
    @patch("apps.trade.utils.create_market_hyperliquid_order.can_open_futures_position", return_value=(True, ""))
    @patch("apps.trade.utils.create_market_hyperliquid_order.get_hyperliquid_last_price", return_value=110.0)
    @patch("apps.trade.utils.create_market_hyperliquid_order.make_hyperliquid_exchange")
    @patch("apps.trade.utils.create_market_hyperliquid_order.UserHyperLiquidKey")
    def test_limit_rests_then_activates(self, _key, make_ex, _price, _allowed, trigger):
        ex = _exchange()
        ex.markets = {"ABC/USDC:USDC": {}}
        ex.fetch_balance.return_value = {"free": {"USDC": 1000.0}}
        ex.create_order.return_value = {"id": 77}
        make_ex.return_value = ex

        ok = create_hyperliquid_future_order(
            "buy", "ABCUSDC", self.user, sl=90, tp=130, order_type="limit", entry=100
        )
        self.assertTrue(ok)
        o = FutureOrder.objects.get(order_id="77")
        self.assertEqual((o.status, o.exchange), ("OPEN", "HYPERLIQUID"))
        self.assertEqual(float(o.pending_sl), 90.0)
        # 122Moon "both" profile, $1000 free: margin 10% of the 30% pool = $30,
        # x3 leverage = $90 notional at the $100 entry -> 0.9 contracts.
        self.assertEqual(float(o.order_quantity), 0.9)
        self.assertEqual(o.leverage, 3)
        ex.set_leverage.assert_called_with(3, "ABC/USDC:USDC", params={"marginMode": "isolated"})

        activate_filled_hyperliquid_limit_order(ex, o, 1.0, 99.5, 0.01)
        o.refresh_from_db()
        self.assertEqual(o.status, "POSITION")
        self.assertEqual(float(o.entry_price), 99.5)
        self.assertEqual(o.tps.count(), 1)
        self.assertEqual(trigger.call_count, 2)  # SL + TP

    @patch("apps.trade.utils.create_market_hyperliquid_order.get_hyperliquid_last_price", return_value=110.0)
    @patch("apps.trade.utils.create_market_hyperliquid_order.make_hyperliquid_exchange")
    @patch("apps.trade.utils.create_market_hyperliquid_order.UserHyperLiquidKey")
    def test_binance_settings_do_not_change_hl_order(self, _key, make_ex, _price):
        TradeSettings.objects.filter(user=self.user).update(
            futures_leverage=1, futures_position_pct=1, futures_max_long=0, futures_max_positions=0
        )
        ex = _exchange()
        ex.markets = {"ABC/USDC:USDC": {}}
        ex.fetch_balance.return_value = {"free": {"USDC": 1000.0}}
        ex.create_order.return_value = {"id": 78}
        make_ex.return_value = ex
        self.assertTrue(create_hyperliquid_future_order("buy", "ABCUSDC", self.user, order_type="limit", entry=100))
        o = FutureOrder.objects.get(order_id="78")
        self.assertEqual((float(o.order_quantity), o.leverage), (0.9, 3))

    def test_invalid_limit_entry_refused(self):
        self.assertFalse(
            create_hyperliquid_future_order("buy", "ABCUSDC", self.user, order_type="limit", entry=None)
        )


class HyperliquidSpotLimitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hs", password="x")
        TradeSettings.get_for_user(self.user)
        # HyperLiquid entries are gated/sized by the 122Moon snapshot.
        add_hl_key(self.user)
        market_risk.ingest_payload(make_payload())

    @patch("apps.trade.utils.create_market_hyperliquid_spot_order.can_open_spot_position", return_value=(True, ""))
    @patch("apps.trade.utils.create_market_hyperliquid_spot_order.get_hyperliquid_last_price", return_value=110.0)
    @patch("apps.trade.utils.create_market_hyperliquid_spot_order.make_hyperliquid_exchange")
    @patch("apps.trade.utils.create_market_hyperliquid_spot_order.UserHyperLiquidKey")
    def test_limit_buy_rests_then_activates(self, _key, make_ex, _price, _allowed):
        from apps.trade.utils.create_market_hyperliquid_spot_order import (
            create_hyperliquid_spot_order,
            activate_filled_hyperliquid_spot_limit_order,
        )

        ex = _exchange()
        ex.markets = {"ABC/USDC": {}}
        ex.fetch_balance.return_value = {"free": {"USDC": 1000.0}}
        ex.create_order.return_value = {"id": 5}
        make_ex.return_value = ex

        self.assertTrue(
            create_hyperliquid_spot_order(
                "buy", "ABCUSDC", self.user, tp=130, dca=80, order_type="limit", entry=100
            )
        )
        o = SpotOrder.objects.get(order_id="5")
        self.assertEqual((o.status, o.exchange), ("OPEN", "HYPERLIQUID"))
        self.assertEqual(float(o.pending_dca), 80.0)
        # 15% of the 55% spot pool of $1000 = $82.50 -> 0.825 at $100.
        self.assertEqual(float(o.order_quantity), 0.825)

        ex.create_order.return_value = {"id": 6}
        activate_filled_hyperliquid_spot_limit_order(ex, o, 2.0, 99.0, 0.02)
        o.refresh_from_db()
        self.assertEqual(o.status, "POSITION")
        self.assertEqual(float(o.final_quantity), 2.0)
        self.assertEqual(SpotTakeProfit.objects.filter(order=o).count(), 1)
        self.assertEqual(o.dca_status, "POSITION")
