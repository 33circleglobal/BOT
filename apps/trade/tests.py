import math
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import UserKey
from apps.trade.models import FutureOrder, SpotOrder, SpotTakeProfit, TradeSettings
from apps.trade.utils.common import split_spot_order_fees
from apps.trade.utils.refresh_positions import refresh_spot_order
from apps.trade.utils.risk_guard import can_open_futures_position, can_open_spot_position

User = get_user_model()


class SplitSpotOrderFeesTests(TestCase):
    """Unit tests for the helper that decides whether a Binance commission
    actually reduces the base-asset quantity received, per Binance's actual
    executedQty/commission semantics (executedQty is always gross; the fee
    is reported separately and may be charged in the base asset, the quote
    asset, or BNB)."""

    def test_fee_in_base_asset_reduces_base_quantity(self):
        order = {"fees": [{"cost": "0.02", "currency": "BTC"}]}
        base_fee, quote_value = split_spot_order_fees(order, "BTC", "USDT", 100.0)
        self.assertEqual(base_fee, 0.02)
        self.assertAlmostEqual(quote_value, 2.0)  # 0.02 BTC * 100 USDT

    def test_fee_in_quote_asset_does_not_reduce_base_quantity(self):
        order = {"fees": [{"cost": "0.5", "currency": "USDT"}]}
        base_fee, quote_value = split_spot_order_fees(order, "BTC", "USDT", 100.0)
        self.assertEqual(base_fee, 0.0)
        self.assertEqual(quote_value, 0.5)

    def test_fee_in_bnb_does_not_reduce_base_quantity(self):
        order = {"fees": [{"cost": "0.0001", "currency": "BNB"}]}
        base_fee, quote_value = split_spot_order_fees(order, "BTC", "USDT", 100.0)
        self.assertEqual(base_fee, 0.0)
        # Can't convert BNB without another price lookup; approximated as
        # its raw cost rather than dropped entirely.
        self.assertEqual(quote_value, 0.0001)

    def test_multiple_fills_mixed_commission_assets(self):
        order = {
            "fees": [
                {"cost": "0.01", "currency": "BTC"},
                {"cost": "0.02", "currency": "BTC"},
                {"cost": "0.05", "currency": "BNB"},
            ]
        }
        base_fee, quote_value = split_spot_order_fees(order, "BTC", "USDT", 100.0)
        self.assertAlmostEqual(base_fee, 0.03)
        self.assertAlmostEqual(quote_value, 0.03 * 100.0 + 0.05)

    def test_falls_back_to_single_fee_field(self):
        order = {"fee": {"cost": "0.02", "currency": "BTC"}}
        base_fee, _ = split_spot_order_fees(order, "BTC", "USDT", 100.0)
        self.assertEqual(base_fee, 0.02)

    def test_no_fee_reported(self):
        order = {}
        base_fee, quote_value = split_spot_order_fees(order, "BTC", "USDT", 100.0)
        self.assertEqual(base_fee, 0.0)
        self.assertEqual(quote_value, 0.0)


class FakeSpotExchange:
    """Minimal stand-in for the ccxt Binance spot exchange used by
    refresh_spot_order. Only implements what that code path touches."""

    def __init__(self, symbol="BTC/USDT", step_decimals=4, dca_order=None, tp_orders=None):
        self.symbol = symbol
        self.step_decimals = step_decimals
        self._dca_order = dca_order or {}
        self._tp_orders = tp_orders or {}
        self.created_orders = []
        self.cancelled_ids = []

    def amountToPrecision(self, symbol, amount):
        factor = 10 ** self.step_decimals
        # ccxt always truncates (rounds down) for amount precision.
        return str(math.floor(float(amount) * factor) / factor)

    def fetch_order(self, id, symbol):
        if id == self._dca_order.get("id"):
            return self._dca_order
        return self._tp_orders.get(id, {"status": "open", "filled": 0})

    def cancel_order(self, id, symbol):
        self.cancelled_ids.append(id)
        return {"id": id, "status": "canceled"}

    def create_order(self, symbol, side, type, amount=None, price=None, params=None):
        order = {
            "id": f"new-{len(self.created_orders)}",
            "symbol": symbol,
            "side": side,
            "type": type,
            "amount": amount,
            "price": price,
            "average": price if price is not None else 100.0,
        }
        self.created_orders.append(order)
        return order


class RefreshSpotOrderDcaFeeTests(TestCase):
    """Regression tests for the exact scenario in the bug report: DCA fills
    must be reduced by any commission actually paid in the base asset before
    being added to the position's sellable (final_quantity) balance, while
    the gross order_quantity (used for the average-entry-price calc) stays
    the sum of gross fills."""

    def setUp(self):
        self.user = User.objects.create_user(username="dca-user", password="x")
        UserKey.objects.create(
            user=self.user, api_key="key", api_secret="secret", is_active=True
        )

    def _make_order(self, **overrides):
        defaults = dict(
            order_id="entry-1",
            symbol="BTC/USDT",
            direction=SpotOrder.TradeDirection.LONG,
            order_quantity=Decimal("2.1"),
            final_quantity=Decimal("2.1"),
            entry_price=Decimal("100"),
            is_spot=True,
            exchange=SpotOrder.ExchangeType.BINANCE,
            status=SpotOrder.TradeStatus.POSITION,
            dca_order_id="dca-1",
            dca_price=Decimal("95"),
            dca_quantity=Decimal("2.2"),
            dca_status=SpotOrder.TradeStatus.POSITION,
            user=self.user,
        )
        defaults.update(overrides)
        return SpotOrder.objects.create(**defaults)

    def _run(self, order, dca_order):
        fake_ex = FakeSpotExchange(symbol=order.symbol, dca_order=dca_order)
        with patch(
            "apps.trade.utils.refresh_positions.make_spot_exchange",
            return_value=fake_ex,
        ):
            result = refresh_spot_order(order)
        order.refresh_from_db()
        return result, fake_ex

    def test_base_asset_fee_reduces_sellable_quantity(self):
        # Exactly the numbers from the bug report: 2.1 existing + 2.2 filled,
        # 0.02 BTC commission -> 4.28 sellable, not 4.3.
        order = self._make_order()
        dca_order = {
            "id": "dca-1",
            "status": "closed",
            "filled": "2.2",
            "average": "95",
            "fees": [{"cost": "0.02", "currency": "BTC"}],
        }
        result, _ = self._run(order, dca_order)

        self.assertTrue(result)
        self.assertEqual(order.order_quantity, Decimal("4.3"))
        self.assertEqual(order.final_quantity, Decimal("4.28"))
        self.assertEqual(order.dca_status, SpotOrder.TradeStatus.CLOSED)

    def test_quote_asset_fee_does_not_reduce_sellable_quantity(self):
        order = self._make_order()
        dca_order = {
            "id": "dca-1",
            "status": "closed",
            "filled": "2.2",
            "average": "95",
            "fees": [{"cost": "0.5", "currency": "USDT"}],
        }
        result, _ = self._run(order, dca_order)

        self.assertTrue(result)
        self.assertEqual(order.order_quantity, Decimal("4.3"))
        # No BTC was actually deducted -> full gross amount is sellable.
        self.assertEqual(order.final_quantity, Decimal("4.3"))

    def test_bnb_fee_does_not_reduce_sellable_quantity(self):
        order = self._make_order()
        dca_order = {
            "id": "dca-1",
            "status": "closed",
            "filled": "2.2",
            "average": "95",
            "fees": [{"cost": "0.001", "currency": "BNB"}],
        }
        result, _ = self._run(order, dca_order)

        self.assertTrue(result)
        self.assertEqual(order.final_quantity, Decimal("4.3"))

    def test_multiple_fills_mixed_commission_assets(self):
        order = self._make_order()
        dca_order = {
            "id": "dca-1",
            "status": "closed",
            "filled": "2.2",
            "average": "95",
            "fees": [
                {"cost": "0.01", "currency": "BTC"},
                {"cost": "0.3", "currency": "USDT"},
            ],
        }
        result, _ = self._run(order, dca_order)

        self.assertTrue(result)
        # Only the BTC-denominated leg reduces sellable quantity.
        self.assertEqual(order.final_quantity, Decimal("4.29"))

    def test_uses_actual_filled_amount_not_requested_amount(self):
        # If Binance reports a different executed amount than what we asked
        # for (e.g. after exchange-side precision), the fill's own reported
        # `filled` is authoritative, not our stored request (dca_quantity).
        order = self._make_order(dca_quantity=Decimal("2.2"))
        dca_order = {
            "id": "dca-1",
            "status": "closed",
            "filled": "2.19",
            "average": "95",
            "fees": [{"cost": "0.01", "currency": "BTC"}],
        }
        result, _ = self._run(order, dca_order)

        self.assertTrue(result)
        self.assertEqual(order.order_quantity, Decimal("2.1") + Decimal("2.19"))
        self.assertEqual(order.final_quantity, Decimal("2.1") + Decimal("2.19") - Decimal("0.01"))

    def test_partial_fill_still_open_is_not_applied_yet(self):
        # A DCA leg that has partially filled but not yet fully closed must
        # not be folded into the position early -- doing so without a way to
        # track "already-applied" quantity would risk double-counting once
        # it eventually fully fills.
        order = self._make_order()
        dca_order = {
            "id": "dca-1",
            "status": "open",
            "filled": "1.0",
            "average": "95",
            "fees": [{"cost": "0.01", "currency": "BTC"}],
        }
        result, _ = self._run(order, dca_order)

        order_status_unchanged = order.dca_status == SpotOrder.TradeStatus.POSITION
        self.assertEqual(order.order_quantity, Decimal("2.1"))
        self.assertEqual(order.final_quantity, Decimal("2.1"))
        self.assertTrue(order_status_unchanged)

    def test_resizes_open_tp_legs_to_new_final_quantity(self):
        order = self._make_order()
        SpotTakeProfit.objects.create(
            order=order,
            tp_order_id="tp-1",
            price=Decimal("110"),
            percent=Decimal("100"),
            quantity=Decimal("2.1"),
            status=SpotTakeProfit.TradeStatus.POSITION,
        )
        dca_order = {
            "id": "dca-1",
            "status": "closed",
            "filled": "2.2",
            "average": "95",
            "fees": [{"cost": "0.02", "currency": "BTC"}],
        }
        result, fake_ex = self._run(order, dca_order)

        self.assertTrue(result)
        tp = order.tps.first()
        tp.refresh_from_db()
        # Resized to the corrected (fee-aware) final_quantity, not the raw
        # gross sum.
        self.assertEqual(float(tp.quantity), 4.28)


class QuickCloseSpotPositionFreeBalanceTests(TestCase):
    """Regression tests for close-time safety: never submit a sell quantity
    larger than what's actually free on the exchange, and always respect the
    LOT_SIZE step size."""

    def setUp(self):
        self.user = User.objects.create_user(username="close-user", password="x")
        UserKey.objects.create(
            user=self.user, api_key="key", api_secret="secret", is_active=True
        )

    def _make_order(self, **overrides):
        defaults = dict(
            order_id="entry-1",
            symbol="BTC/USDT",
            direction=SpotOrder.TradeDirection.LONG,
            order_quantity=Decimal("4.3"),
            final_quantity=Decimal("4.28"),
            entry_price=Decimal("100"),
            is_spot=True,
            exchange=SpotOrder.ExchangeType.BINANCE,
            status=SpotOrder.TradeStatus.POSITION,
            user=self.user,
        )
        defaults.update(overrides)
        return SpotOrder.objects.create(**defaults)

    def test_never_sells_more_than_free_balance(self):
        from apps.trade.utils.close_market_order_spot import quick_close_spot_position

        order = self._make_order()

        class Ex(FakeSpotExchange):
            def market(self, symbol):
                return {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}}

            def fetch_balance(self):
                # Slightly less than final_quantity, as if a base-asset fee
                # shaved a bit more off than our bookkeeping expected.
                return {"free": {"BTC": 4.2799}}

            def fetch_ticker(self, symbol):
                return {"last": 100}

        fake_ex = Ex(symbol="BTC/USDT")
        with patch(
            "apps.trade.utils.close_market_order_spot.make_spot_exchange",
            return_value=fake_ex,
        ):
            ok = quick_close_spot_position(order, self.user)

        self.assertTrue(ok)
        self.assertEqual(len(fake_ex.created_orders), 1)
        sold_amount = fake_ex.created_orders[0]["amount"]
        self.assertLessEqual(sold_amount, 4.2799)

    def test_zero_free_balance_closes_without_selling(self):
        from apps.trade.utils.close_market_order_spot import quick_close_spot_position

        order = self._make_order()

        class Ex(FakeSpotExchange):
            def market(self, symbol):
                return {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}}

            def fetch_balance(self):
                return {"free": {"BTC": 0}}

            def fetch_ticker(self, symbol):
                return {"last": 100}

        fake_ex = Ex(symbol="BTC/USDT")
        with patch(
            "apps.trade.utils.close_market_order_spot.make_spot_exchange",
            return_value=fake_ex,
        ):
            ok = quick_close_spot_position(order, self.user)

        self.assertTrue(ok)
        self.assertEqual(len(fake_ex.created_orders), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, SpotOrder.TradeStatus.CLOSED)

    def test_quantity_is_quantized_to_step_size(self):
        from apps.trade.utils.close_market_order_spot import quick_close_spot_position

        order = self._make_order(final_quantity=Decimal("4.28"))

        class Ex(FakeSpotExchange):
            def market(self, symbol):
                return {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}}

            def fetch_balance(self):
                # Free balance has more decimals than the BTC/USDT step size
                # (4 decimals) allows.
                return {"free": {"BTC": 4.279999}}

            def fetch_ticker(self, symbol):
                return {"last": 100}

        fake_ex = Ex(symbol="BTC/USDT", step_decimals=4)
        with patch(
            "apps.trade.utils.close_market_order_spot.make_spot_exchange",
            return_value=fake_ex,
        ):
            ok = quick_close_spot_position(order, self.user)

        self.assertTrue(ok)
        sold_amount = fake_ex.created_orders[0]["amount"]
        self.assertEqual(sold_amount, 4.2799)
        self.assertLessEqual(sold_amount, 4.279999)


class MarketDataStalenessTests(TestCase):
    """Regression tests for the stale-market-data guard: if the
    "update_market_risk" webhook stops arriving (e.g. TradingView-side
    outage or a dropped HTTP call), the bot must stop opening *new*
    positions rather than keep trading against a possibly-outdated
    bull/bear regime setting."""

    def setUp(self):
        self.user = User.objects.create_user(username="stale-user", password="x")

    def test_never_received_update_is_stale_when_sync_enabled(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.last_market_risk_update = None
        settings_obj.save()

        self.assertTrue(settings_obj.is_market_data_stale())

    def test_recent_update_is_not_stale(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.max_market_data_age_minutes = 30
        settings_obj.last_market_risk_update = timezone.now() - timedelta(minutes=5)
        settings_obj.save()

        self.assertFalse(settings_obj.is_market_data_stale())

    def test_update_older_than_threshold_is_stale(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.max_market_data_age_minutes = 30
        settings_obj.last_market_risk_update = timezone.now() - timedelta(minutes=45)
        settings_obj.save()

        self.assertTrue(settings_obj.is_market_data_stale())

    def test_manual_mode_is_never_stale(self):
        # A user with sync_risk_from_webhook off manages limits by hand and
        # never expects webhook updates -- staleness is meaningless for them.
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = False
        settings_obj.last_market_risk_update = None
        settings_obj.save()

        self.assertFalse(settings_obj.is_market_data_stale())

    def test_stale_data_blocks_new_spot_position(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.last_market_risk_update = timezone.now() - timedelta(hours=2)
        settings_obj.max_market_data_age_minutes = 30
        settings_obj.save()

        allowed, reason = can_open_spot_position(self.user, "BTC/USDT")
        self.assertFalse(allowed)
        self.assertIn("stale", reason.lower())

    def test_stale_data_blocks_new_futures_position(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.last_market_risk_update = timezone.now() - timedelta(hours=2)
        settings_obj.max_market_data_age_minutes = 30
        settings_obj.save()

        allowed, reason = can_open_futures_position(
            self.user, "BTC/USDT", FutureOrder.TradeDirection.LONG
        )
        self.assertFalse(allowed)
        self.assertIn("stale", reason.lower())

    def test_fresh_data_allows_new_position(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.last_market_risk_update = timezone.now() - timedelta(minutes=1)
        settings_obj.max_market_data_age_minutes = 30
        settings_obj.save()

        allowed, _ = can_open_spot_position(self.user, "BTC/USDT")
        self.assertTrue(allowed)

    def test_manual_mode_bypasses_staleness_for_new_positions(self):
        settings_obj = TradeSettings.get_for_user(self.user)
        settings_obj.sync_risk_from_webhook = False
        settings_obj.last_market_risk_update = None
        settings_obj.save()

        allowed, _ = can_open_spot_position(self.user, "BTC/USDT")
        self.assertTrue(allowed)


class MarketRiskWebhookStampsFreshnessTests(TestCase):
    """The 'update_market_risk' webhook action is the only thing that should
    ever mark market data fresh again -- verify it actually stamps
    last_market_risk_update on every synced user's settings."""

    def test_update_market_risk_stamps_last_update(self):
        user = User.objects.create_user(username="webhook-user", password="x")
        settings_obj = TradeSettings.get_for_user(user)
        settings_obj.sync_risk_from_webhook = True
        settings_obj.last_market_risk_update = timezone.now() - timedelta(hours=5)
        settings_obj.save()
        self.assertTrue(settings_obj.is_market_data_stale())

        response = self.client.post(
            "/webhook/",
            data={
                "action": "update_market_risk",
                "regime": "bear",
                "futures": {"max_long": 1, "max_short": 4, "max_total": 5},
                "spot": {"max_trades": 2},
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        settings_obj.refresh_from_db()
        self.assertFalse(settings_obj.is_market_data_stale())
        self.assertIsNotNone(settings_obj.last_market_risk_update)

    def test_update_market_risk_does_not_touch_manual_users(self):
        user = User.objects.create_user(username="manual-user", password="x")
        settings_obj = TradeSettings.get_for_user(user)
        settings_obj.sync_risk_from_webhook = False
        settings_obj.save()

        self.client.post(
            "/webhook/",
            data={
                "action": "update_market_risk",
                "regime": "bear",
                "futures": {"max_long": 1, "max_short": 4, "max_total": 5},
            },
            content_type="application/json",
        )

        settings_obj.refresh_from_db()
        self.assertIsNone(settings_obj.last_market_risk_update)
