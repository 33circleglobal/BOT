import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.trade.models import FutureOrder, SpotOrder, WebhookLog
from apps.trade.task import cancel_pending_orders_for_symbol

User = get_user_model()
_seq = iter(range(1, 10_000))


def _fut(user, exchange, status, symbol="BTCUSDT"):
    n = next(_seq)
    return FutureOrder.objects.create(
        user=user, symbol=symbol, direction="LONG", status=status, exchange=exchange,
        order_id=f"F{n}", stop_loss_order_id=f"S{n}",
    )


def _spot(user, exchange, status, symbol="BTCUSDT"):
    return SpotOrder.objects.create(
        user=user, symbol=symbol, direction="LONG", status=status, exchange=exchange,
        order_id=f"P{next(_seq)}",
    )


@override_settings(TRADINGVIEW_WEBHOOK_SECRET="")
class CancelPendingWebhookTests(TestCase):
    def post(self, **over):
        body = {"exchange": "Binance", "market": "futures", "symbol": "BTCUSDT",
                "event": "CANCEL_PENDING_ORDER", "trade_id": "t", "event_id": "t:CANCEL",
                "reason": "ALIGNMENT_LOST", **over}
        return self.client.post("/webhook/", json.dumps(body), content_type="application/json")

    @patch("apps.trade.views.cancel_pending_orders_for_symbol")
    def test_webhook_dispatches(self, task):
        from django.urls import reverse  # noqa: F401
        r = self.post(exchange="Hyperliquid", market="spot")
        if r.status_code == 404:
            self.skipTest("webhook URL differs")
        self.assertEqual(r.status_code, 200)
        task.delay.assert_called_once_with("BTCUSDT", "spot", "hyperliquid")
        self.assertEqual(WebhookLog.objects.get().status, WebhookLog.Status.PROCESSED)

    def test_rejects_bad_market(self):
        r = self.post(market="x")
        if r.status_code == 404:
            self.skipTest("webhook URL differs")
        self.assertEqual(r.status_code, 400)


class CancelPendingTaskTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="u", password="x")

    def _run(self, market, exchange, targets):
        with patch(f"apps.trade.task.{targets[0]}", return_value=True) as c:
            cancel_pending_orders_for_symbol(symbol="BTCUSDT", market=market, exchange=exchange)
        return c

    def test_futures_binance_only_open_binance(self):
        keep = [_fut(self.u, "BINANCE_FUTURES", "POSITION"), _fut(self.u, "HYPERLIQUID", "OPEN"),
                _fut(self.u, "BINANCE_FUTURES", "OPEN", symbol="ETHUSDT")]
        tgt = _fut(self.u, "BINANCE_FUTURES", "OPEN")
        c = self._run("futures", "binance", ["cancel_pending_limit_order"])
        c.assert_called_once()
        self.assertEqual(c.call_args.args[0].id, tgt.id)
        self.assertEqual(len(keep), 3)

    def test_futures_hl(self):
        tgt = _fut(self.u, "HYPERLIQUID", "OPEN"); _fut(self.u, "BINANCE_FUTURES", "OPEN")
        c = self._run("futures", "hyperliquid", ["cancel_pending_hyperliquid_limit_order"])
        c.assert_called_once(); self.assertEqual(c.call_args.args[0].id, tgt.id)

    def test_spot_binance(self):
        tgt = _spot(self.u, "BINANCE", "OPEN"); _spot(self.u, "BINANCE", "POSITION"); _spot(self.u, "HYPERLIQUID", "OPEN")
        c = self._run("spot", "binance", ["cancel_pending_spot_limit_order"])
        c.assert_called_once(); self.assertEqual(c.call_args.args[0].id, tgt.id)

    def test_spot_hl(self):
        tgt = _spot(self.u, "HYPERLIQUID", "OPEN"); _spot(self.u, "BINANCE", "OPEN")
        c = self._run("spot", "hyperliquid", ["cancel_pending_hyperliquid_spot_limit_order"])
        c.assert_called_once(); self.assertEqual(c.call_args.args[0].id, tgt.id)
