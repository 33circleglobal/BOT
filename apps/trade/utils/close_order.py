from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder, FutureTakeProfit
from apps.trade.utils.common import make_futures_exchange

import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def cancel_algo_order(exchange, symbol, algo_id):
    """
    Cancels a conditional (algo) order via /fapi/v1/algoOrder.
    Regular cancel_order() no longer finds STOP_MARKET/TAKE_PROFIT_MARKET
    orders since they were migrated to the algo endpoint.
    """
    if not algo_id or str(algo_id).startswith("DISABLED-"):
        return None
    try:
        return exchange.fapiprivate_delete_algoorder(
            {
                "symbol": exchange.market_id(symbol),
                "algoId": algo_id,
            }
        )
    except Exception as e:
        # Order may have already triggered/expired — not fatal
        logger.warning(f"Could not cancel algo order {algo_id} for {symbol}: {e}")
        return None


def quick_close_position(order: FutureOrder, user: User):
    try:
        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = make_futures_exchange(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )
        symbol = order.symbol
        quantity = order.order_quantity
        side = "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"

        # Cancel protective SL algo order
        cancel_algo_order(exchange, symbol, order.stop_loss_order_id)

        # Cancel any open TP algo orders tied to this position
        open_tps = FutureTakeProfit.objects.filter(
            order=order, status=FutureTakeProfit.TradeStatus.POSITION
        )
        for tp in open_tps:
            cancel_algo_order(exchange, symbol, tp.tp_order_id)
        open_tps.update(status=FutureTakeProfit.TradeStatus.CANCELLED)

        close_order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=quantity,
            params={"reduceOnly": True},
        )
        close_order = exchange.fetch_order(
            close_order["id"],
            symbol,
        )
        exit_avg = float(close_order.get("average") or 0)

        order.status = FutureOrder.TradeStatus.CLOSED
        # Mark SL as cancelled if we closed manually via market
        order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED

        # Fee info may be missing depending on exchange response
        fee = close_order.get("fee") or {}
        fee_cost = fee.get("cost", 0)
        order.total_fee = float(order.total_fee or 0) + float(fee_cost)

        if order.direction == FutureOrder.TradeDirection.LONG:
            entry_price = float(order.entry_price)
            pnl = float(exit_avg - entry_price) * float(quantity)
            order.pnl = pnl
        else:
            entry_price = float(order.entry_price)
            pnl = float(entry_price - exit_avg) * float(quantity)
            order.pnl = pnl
        order.pnl_percentage = (float(order.pnl) / float(order.entry_price)) * 100
        order.save()
        logger.info(f"Order closed successfully for user {user.username}")
        return True
    except Exception as e:
        logger.error(
            f"Error closing futures position for {user.username}: {e}", exc_info=True
        )
        return False
