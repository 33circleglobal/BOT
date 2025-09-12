from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder
from django.utils import timezone

import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_connection_with_ccxt(api_key, api_secret):
    exchange = ccxt.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
        }
    )
    exchange.load_markets()
    return exchange


def refresh_orders():
    orders = FutureOrder.objects.filter(status=FutureOrder.TradeStatus.POSITION)

    for order in orders:
        user = order.user
        quantity = float(order.order_quantity)
        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = create_connection_with_ccxt(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        # Check SL first
        if order.stop_loss_order_id:
            try:
                sl_info = exchange.fetch_order(id=order.stop_loss_order_id, symbol=order.symbol)
                if sl_info.get("remaining") == 0 and sl_info.get("status") == "closed":
                    order.stop_loss_price = sl_info.get("average") or sl_info.get("price")
                    order.status = FutureOrder.TradeStatus.CLOSED
                    order.stop_loss_status = FutureOrder.TradeStatus.CLOSED
                    fee = sl_info.get("fee") or {}
                    order.stop_loss_fee = float(fee.get("cost", 0))
                    order.total_fee = float(order.total_fee or 0) + float(fee.get("cost", 0))
                    order.tp_status = FutureOrder.TradeStatus.CANCELLED

                    entry_price = float(order.entry_price)
                    exit_price = float(order.stop_loss_price)
                    if order.direction == FutureOrder.TradeDirection.LONG:
                        order.pnl = (exit_price - entry_price) * quantity
                    else:
                        order.pnl = (entry_price - exit_price) * quantity
                    order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
                    order.closed_at = timezone.now()
                    order.save()
                    continue
            except Exception:
                pass

        # Check TP
        if order.tp_order_id:
            try:
                tp_info = exchange.fetch_order(id=order.tp_order_id, symbol=order.symbol)
                if tp_info.get("remaining") == 0 and tp_info.get("status") == "closed":
                    order.tp_price = tp_info.get("average") or tp_info.get("price")
                    order.status = FutureOrder.TradeStatus.CLOSED
                    order.tp_status = FutureOrder.TradeStatus.CLOSED
                    fee = tp_info.get("fee") or {}
                    order.tp_fee = float(fee.get("cost", 0))
                    order.total_fee = float(order.total_fee or 0) + float(fee.get("cost", 0))
                    order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED

                    entry_price = float(order.entry_price)
                    exit_price = float(order.tp_price)
                    if order.direction == FutureOrder.TradeDirection.LONG:
                        order.pnl = (exit_price - entry_price) * quantity
                    else:
                        order.pnl = (entry_price - exit_price) * quantity
                    order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
                    order.closed_at = timezone.now()
                    order.save()
                    continue
            except Exception:
                pass
