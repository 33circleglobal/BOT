from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder, FutureTakeProfit
from apps.trade.utils.common import (
    make_futures_exchange,
    get_symbol_last_price,
    compute_default_sl,
    opposite_side,
)

import ccxt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def set_margin_mode(exchange, symbol, margin_mode):
    try:
        info = exchange.fapiprivatev2_get_positionrisk(
            {"symbol": exchange.market_id(symbol)}
        )[0]
        current_margin_mode = info["marginType"]
        if margin_mode != current_margin_mode:
            exchange.fapiPrivatePostMarginType(
                {
                    "symbol": exchange.market_id(symbol),
                    "marginType": margin_mode.upper(),
                }
            )
    except Exception as e:
        logger.error(f"Error setting margin mode for {symbol}: {e}")
        return False


def apply_leverage(exchange, symbol, leverage):
    try:
        exchange.fapiprivate_post_leverage(
            {
                "symbol": exchange.market_id(symbol),
                "leverage": leverage,
            }
        )
    except Exception as e:
        logger.error(f"Error applying leverage for {symbol}: {e}")
        return False


def create_binance_future_order(
    side: str,
    symbol: str,
    user: User,
    *,
    sl: float | None = None,
    tp: float | None = None,
    tps: list | None = None,
    leverage: int = 5,
    position_pct: float = 90.0,
):
    try:
        margin_mode = "isolated"
        side = side.lower()
        position = position_pct

        user_binance_key = UserKey.objects.get(user=user, is_active=True)
        exchange = make_futures_exchange(
            api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
        )

        current_price_of_symbol = get_symbol_last_price(exchange, symbol)
        balance = exchange.fetch_balance()
        user_balance = balance["free"]["USDT"]

        user_usable_balance = (user_balance * position / 100) * leverage
        print(user_usable_balance, current_price_of_symbol)
        quantity = user_usable_balance / current_price_of_symbol
        quantity = exchange.amountToPrecision(symbol, quantity)

        set_margin_mode(exchange, symbol, margin_mode)
        apply_leverage(exchange, symbol, leverage)

        order = exchange.create_order(
            symbol=symbol, side=side, type="market", amount=quantity
        )

        inv_side = opposite_side(side)
        # Validate manual SL/TP against current price to avoid immediate triggers
        cur = float(current_price_of_symbol)
        if sl is not None:
            s = float(sl)
            if (side == "buy" and s >= cur) or (side == "sell" and s <= cur):
                raise ValueError("Invalid SL relative to current price")
        if tp is not None:
            t = float(tp)
            if (side == "buy" and t <= cur) or (side == "sell" and t >= cur):
                raise ValueError("Invalid TP relative to current price")

        # Stop loss: provided or default 1%
        stop_price = (
            float(sl) if sl is not None else compute_default_sl(order["average"], side)
        )
        sl_order = exchange.create_order(
            symbol=symbol,
            side=inv_side,
            type="STOP_MARKET",
            amount=quantity,
            params={
                "stopPrice": float(exchange.priceToPrecision(symbol, stop_price)),
                "reduceOnly": True,
            },
        )

        # Optional single TP or multiple TPs
        tp_order = None
        created_tps = []
        base_qty = float(quantity)
        if tps:
            # tps expected as list of dicts: {"price": float, "percent": float}
            market_info = exchange.market(symbol)
            min_amount = float(
                (market_info.get("limits") or {}).get("amount", {}).get("min") or 0
            )
            min_cost = float(
                (market_info.get("limits") or {}).get("cost", {}).get("min") or 0
            )
            for tp_def in tps:
                try:
                    p = float(tp_def.get("price"))
                    pct = float(tp_def.get("percent"))
                except Exception:
                    continue
                if pct <= 0:
                    continue
                # Validate direction
                if side == "buy" and p <= cur:
                    raise ValueError("TP must be above current for long")
                if side == "sell" and p >= cur:
                    raise ValueError("TP must be below current for short")
                part_qty = base_qty * (pct / 100.0)
                part_qty_p = float(exchange.amountToPrecision(symbol, part_qty))
                stop_p = float(exchange.priceToPrecision(symbol, p))
                if part_qty_p <= 0:
                    continue
                if min_amount and part_qty_p < min_amount:
                    continue
                if min_cost and (part_qty_p * stop_p) < min_cost:
                    continue
                try:
                    tp_o = exchange.create_order(
                        symbol=symbol,
                        side=inv_side,
                        type="TAKE_PROFIT_MARKET",
                        amount=part_qty_p,
                        params={"stopPrice": stop_p, "reduceOnly": True},
                    )
                    created_tps.append(
                        {
                            "id": tp_o.get("id", ""),
                            "price": stop_p,
                            "percent": pct,
                            "qty": part_qty_p,
                        }
                    )
                except Exception:
                    continue
        elif tp is not None:
            # Map single TP to a child TP covering 100%
            tp_price = float(exchange.priceToPrecision(symbol, float(tp)))
            tp_o = exchange.create_order(
                symbol=symbol,
                side=inv_side,
                type="TAKE_PROFIT_MARKET",
                amount=quantity,
                params={"stopPrice": tp_price, "reduceOnly": True},
            )
            created_tps.append(
                {
                    "id": tp_o.get("id", ""),
                    "price": tp_price,
                    "percent": 100.0,
                    "qty": float(quantity),
                }
            )

        position_direction = (
            FutureOrder.TradeDirection.LONG
            if side == "buy"
            else FutureOrder.TradeDirection.SHORT
        )

        fee = order.get("fee") or {}
        entry_fee = fee.get("cost", 0)
        entry_fee_currency = fee.get("currency", "USDT")
        total_fee = fee.get("cost", 0)

        stop_loss_price = (
            sl_order.get("price")
            or sl_order.get("stopPrice")
            or sl_order.get("triggerPrice")
        )

        fobj = FutureOrder.objects.create(
            order_id=order["id"],
            symbol=symbol,
            direction=position_direction,
            leverage=5,
            order_quantity=quantity,
            entry_price=order["average"],
            entry_fee=entry_fee,
            entry_fee_currency=entry_fee_currency,
            total_fee=total_fee,
            stop_loss_order_id=sl_order["id"],
            stop_loss_price=stop_loss_price,
            user=user,
        )

        # Persist multiple TP children if any
        if created_tps:
            for item in created_tps:
                FutureTakeProfit.objects.create(
                    order=fobj,
                    tp_order_id=item["id"],
                    price=item["price"],
                    percent=item["percent"],
                    quantity=item["qty"],
                    status=FutureTakeProfit.TradeStatus.POSITION,
                )

        return True

    except Exception as e:
        logger.error(
            f"Error creating futures order for {user.username}: {e}", exc_info=True
        )
        return False
