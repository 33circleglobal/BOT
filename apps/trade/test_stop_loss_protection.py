from unittest.mock import MagicMock, patch

import ccxt
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.trade.models import FutureOrder
from apps.trade.task import refresh_pending_futures_orders
from apps.trade.utils.common import compute_default_sl
from apps.trade.utils.create_market_hyperliquid_order import (
    place_hyperliquid_futures_protection,
)
from apps.trade.utils.create_market_order import place_futures_protection
from apps.trade.utils.locks import pending_order_lock
from apps.trade.utils.refresh_positions import refresh_futures_order

User = get_user_model()

BINANCE = "apps.trade.utils.create_market_order"
HL = "apps.trade.utils.create_market_hyperliquid_order"
REFRESH = "apps.trade.utils.refresh_positions"


def _exchange():
    ex = MagicMock()
    ex.amountToPrecision.side_effect = lambda s, a: str(float(a))
    ex.priceToPrecision.side_effect = lambda s, p: str(float(p))
    ex.market.return_value = {"limits": {"amount": {"min": 1}, "cost": {"min": 1}}}
    return ex


def _short(user, **kw):
    fields = dict(
        order_id="E1",
        symbol="ABCUSDT",
        direction=FutureOrder.TradeDirection.SHORT,
        leverage=3,
        order_quantity=3000,
        entry_price="0.00385",
        stop_loss_order_id="PENDING-1",
        stop_loss_price=0,
        stop_loss_status=FutureOrder.TradeStatus.CANCELLED,
        user=user,
    )
    fields.update(kw)
    return FutureOrder.objects.create(**fields)


def _fake_close(order, user):
    order.status = FutureOrder.TradeStatus.CLOSED
    order.save()
    return True


@override_settings(DISABLE_FUTURES_STOP_LOSS=False)
class BinanceStopPlacementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sl", password="x")
        self.order = _short(self.user)

    def _place(self):
        place_futures_protection(
            _exchange(), self.order, "sell", "3000", 0.00385, 0.004003, None,
            [{"price": 0.0035, "percent": 100}], self.user,
        )
        self.order.refresh_from_db()

    @patch("apps.trade.utils.close_order.quick_close_position", side_effect=_fake_close)
    @patch(f"{BINANCE}.create_algo_order", side_effect=ccxt.OrderImmediatelyFillable("-2021"))
    def test_breached_stop_closes_position_and_skips_tps(self, algo, close):
        self._place()
        close.assert_called_once()
        self.assertEqual(algo.call_count, 1)  # SL only, no TP
        self.assertEqual(self.order.status, "CLOSED")
        self.assertEqual(self.order.stop_loss_status, "CLOSED")
        self.assertEqual(float(self.order.stop_loss_price), 0.004003)
        self.assertIsNotNone(self.order.closed_at)

    @patch("apps.trade.utils.close_order.quick_close_position", return_value=False)
    @patch(f"{BINANCE}.create_algo_order")
    def test_failed_close_stays_retryable_and_keeps_tps(self, algo, _close):
        algo.side_effect = [ccxt.OrderImmediatelyFillable("-2021"), {"id": "TP1"}]
        self._place()
        self.assertEqual(self.order.status, "POSITION")
        self.assertEqual(self.order.stop_loss_status, "FAILED")
        self.assertEqual(self.order.tps.count(), 1)

    @patch("apps.trade.utils.close_order.quick_close_position")
    @patch(f"{BINANCE}.create_algo_order")
    def test_other_error_marks_failed_without_closing(self, algo, close):
        algo.side_effect = [ccxt.NetworkError("timeout"), {"id": "TP1"}]
        self._place()
        close.assert_not_called()
        self.assertEqual(self.order.status, "POSITION")
        self.assertEqual(self.order.stop_loss_status, "FAILED")
        self.assertEqual(float(self.order.stop_loss_price), 0.004003)

    @override_settings(DISABLE_FUTURES_STOP_LOSS=True)
    @patch(f"{BINANCE}.create_algo_order", return_value={"id": "TP1"})
    def test_disabled_stop_is_still_cancelled(self, _algo):
        self._place()
        self.assertEqual(self.order.stop_loss_status, "CANCELLED")


@override_settings(DISABLE_FUTURES_STOP_LOSS=False)
class BinanceStopRetryTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="rt", password="x")
        self.order = _short(
            user,
            stop_loss_order_id="DISABLED-1",
            stop_loss_price="0.004003",
            stop_loss_status=FutureOrder.TradeStatus.FAILED,
        )

    def _refresh(self):
        refresh_futures_order(self.order)
        self.order.refresh_from_db()

    @patch(f"{REFRESH}.create_algo_order", return_value={"id": "SL2"})
    @patch(f"{REFRESH}.make_futures_exchange", return_value=_exchange())
    @patch(f"{REFRESH}.UserKey")
    def test_failed_stop_with_no_filled_tp_is_retried(self, _key, _ex, _algo):
        self._refresh()
        self.assertEqual(self.order.stop_loss_status, "POSITION")
        self.assertEqual(self.order.stop_loss_order_id, "SL2")

    @patch("apps.trade.utils.close_order.quick_close_position", side_effect=_fake_close)
    @patch(f"{REFRESH}.create_algo_order", side_effect=ccxt.OrderImmediatelyFillable("-2021"))
    @patch(f"{REFRESH}.make_futures_exchange", return_value=_exchange())
    @patch(f"{REFRESH}.UserKey")
    def test_retry_closes_if_stop_now_breached(self, _key, _ex, _algo, close):
        self._refresh()
        close.assert_called_once()
        self.assertEqual(self.order.status, "CLOSED")
        self.assertEqual(self.order.stop_loss_status, "CLOSED")


@override_settings(DISABLE_FUTURES_STOP_LOSS=False)
class HyperliquidStopPlacementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hlsl", password="x")
        self.order = _short(self.user, exchange=FutureOrder.ExchangeType.HYPERLIQUID)

    def _place(self, price):
        with patch(f"{HL}.create_trigger_order", side_effect=ccxt.InvalidOrder("no")), \
             patch(f"{HL}.get_hyperliquid_last_price", return_value=price), \
             patch(
                 "apps.trade.utils.close_order_hyperliquid.quick_close_hyperliquid_position",
                 side_effect=_fake_close,
             ) as close:
            place_hyperliquid_futures_protection(
                _exchange(), self.order, "ABC/USDC:USDC", "sell", "3000", 0.00385,
                0.004003, None, None, self.user,
            )
        self.order.refresh_from_db()
        return close

    def test_breached_stop_closes_position(self):
        self._place(0.00408).assert_called_once()
        self.assertEqual(self.order.status, "CLOSED")

    def test_unbreached_or_unknown_price_marks_failed(self):
        for price in (0.0039, None):
            self._place(price).assert_not_called()
            self.assertEqual(self.order.stop_loss_status, "FAILED")


class PendingOrderTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="p", password="x")
        self.pending = _short(user, status=FutureOrder.TradeStatus.OPEN)
        _short(user, order_id="E2", stop_loss_order_id="SL-2")

    def test_only_resting_orders_are_dispatched(self):
        with patch("apps.trade.task.refresh_single_future_position.apply_async") as send:
            refresh_pending_futures_orders()
        send.assert_called_once_with(args=[self.pending.id], expires=60)

    @patch(f"{REFRESH}._refresh_pending_limit_order")
    def test_locked_order_is_not_processed_twice(self, inner):
        with pending_order_lock(self.pending.id) as acquired:
            self.assertTrue(acquired)
            self.assertFalse(refresh_futures_order(self.pending))
        inner.assert_not_called()
        refresh_futures_order(self.pending)
        inner.assert_called_once()

    @patch(f"{REFRESH}._refresh_pending_limit_order")
    def test_stale_object_is_reloaded(self, inner):
        FutureOrder.objects.filter(id=self.pending.id).update(status="CANCELLED")
        self.assertFalse(refresh_futures_order(self.pending))
        inner.assert_not_called()


class DefaultStopTests(TestCase):
    def test_low_priced_coin_keeps_precision(self):
        self.assertAlmostEqual(compute_default_sl(0.000048, "sell"), 0.00004812, places=10)
        self.assertAlmostEqual(compute_default_sl(0.00385, "sell"), 0.003859625, places=9)
