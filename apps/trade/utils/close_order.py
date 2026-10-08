from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder, FutureTakeProfit
from apps.trade.utils.common import make_futures_exchange

import ccxt
import logging
from decimal import Decimal

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
        full_quantity = order.order_quantity
        side = "sell" if order.direction == FutureOrder.TradeDirection.LONG else "buy"

        # Any TPs that already filled banked their own realized PnL on their
        # own leg quantity — only what's left of the position should be sent
        # to the exchange, and only that remaining quantity's PnL should be
        # computed here, on top of (not instead of) what those legs realized.
        closed_tps = FutureTakeProfit.objects.filter(
            order=order, status=FutureTakeProfit.TradeStatus.CLOSED
        )
        realized_tp_qty_dec = sum(
            (Decimal(str(c.quantity)) for c in closed_tps), Decimal("0")
        )
        realized_tp_qty = float(realized_tp_qty_dec)
        entry_price = float(order.entry_price)
        realized_tp_pnl = sum(
            (
                (float(c.price) - entry_price) * float(c.quantity)
                if order.direction == FutureOrder.TradeDirection.LONG
                else (entry_price - float(c.price)) * float(c.quantity)
                for c in closed_tps
            ),
            0.0,
        )
        # Decimal subtraction (not float) avoids binary rounding error here —
        # e.g. float(0.83) - float(0.33) == 0.49999999999999994, which
        # amountToPrecision then truncates a further step down to 0.49,
        # leaving real dust open on the exchange after this order is marked
        # CLOSED below. amountToPrecision is still applied afterward to round
        # to the symbol's actual step size.
        quantity_dec = Decimal(str(full_quantity)) - realized_tp_qty_dec
        if quantity_dec <= 0:
            quantity_dec = Decimal(str(full_quantity))
        quantity = float(exchange.amountToPrecision(symbol, float(quantity_dec)))

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
        print(close_order)
        exit_avg = float(close_order.get("average") or 0)

        # Fee info may be missing depending on exchange response
        fee = close_order.get("fee") or {}
        total_fee = float(order.total_fee or 0) + float(fee.get("cost", 0))

        if order.direction == FutureOrder.TradeDirection.LONG:
            close_leg_pnl = float(exit_avg - entry_price) * quantity
        else:
            close_leg_pnl = float(entry_price - exit_avg) * quantity

        # The market order above is rounded to the symbol's step size, so it
        # can undershoot the true remaining size by a step (this is exactly
        # how the float-precision bug this replaces used to leave dust open
        # — see git history). Re-check the exchange's own reported position
        # size and sweep anything still open with a second reduce-only
        # market order rather than marking this CLOSED while a real,
        # untracked residual position stays live on the exchange.
        try:
            positions = exchange.fetch_positions([symbol])
            market_symbol = exchange.market(symbol)["symbol"]
            residual = 0.0
            for p in positions:
                if p.get("symbol") == market_symbol:
                    residual = abs(float(p.get("contracts") or 0))
                    break
            residual = float(exchange.amountToPrecision(symbol, residual))
        except Exception as e:
            logger.warning(
                f"[close] Could not verify residual position for {symbol} "
                f"after closing order {order.id}: {e}"
            )
            residual = 0.0

        if residual > 0:
            logger.warning(
                f"[close] {symbol} left {residual} open after closing order "
                f"{order.id} (requested {quantity}); sweeping residual with a "
                f"follow-up reduce-only order."
            )
            try:
                sweep_order = exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=residual,
                    params={"reduceOnly": True},
                )
                sweep_order = exchange.fetch_order(sweep_order["id"], symbol)
                sweep_avg = float(sweep_order.get("average") or exit_avg)
                sweep_fee = float((sweep_order.get("fee") or {}).get("cost", 0))
                total_fee += sweep_fee
                if order.direction == FutureOrder.TradeDirection.LONG:
                    close_leg_pnl += (sweep_avg - entry_price) * residual
                else:
                    close_leg_pnl += (entry_price - sweep_avg) * residual
                quantity += residual
            except Exception as e:
                # The position is still genuinely open on the exchange — do
                # not mark this order CLOSED, so the next manual close or
                # refresh cycle retries instead of losing track of it.
                logger.error(
                    f"[close] Failed to sweep {residual} residual {symbol} "
                    f"for order {order.id}: {e}",
                    exc_info=True,
                )
                order.total_fee = total_fee
                order.pnl = realized_tp_pnl + close_leg_pnl
                order.save()
                return False

        order.status = FutureOrder.TradeStatus.CLOSED
        # Mark SL as cancelled if we closed manually via market
        order.stop_loss_status = FutureOrder.TradeStatus.CANCELLED
        order.total_fee = total_fee
        order.pnl = realized_tp_pnl + close_leg_pnl

        # ROE% = PnL / initial margin, where initial margin = notional / leverage
        # (matches Binance's displayed PnL% for a leveraged futures position).
        # Margin is always based on the ORIGINAL full position size, not what's
        # left to close now.
        leverage = float(order.leverage or 1)
        notional = entry_price * float(full_quantity)
        margin = notional / leverage if leverage else notional
        order.pnl_percentage = (float(order.pnl) / margin) * 100 if margin else 0

        order.save()
        logger.info(f"Order closed successfully for user {user.username}")
        return True
    except Exception as e:
        logger.error(
            f"Error closing futures position for {user.username}: {e}", exc_info=True
        )
        return False
