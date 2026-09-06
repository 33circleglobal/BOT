from apps.accounts.models import User, UserKey
from apps.trade.models import FutureOrder, FutureTakeProfit
from apps.trade.utils.common import (
    make_futures_exchange,
    get_symbol_last_price,
    compute_default_sl,
    opposite_side,
)
from apps.trade.utils.risk_guard import can_open_futures_position

import ccxt
import logging
from django.conf import settings
from uuid import uuid4

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


def create_algo_order(
    exchange,
    symbol,
    side,
    order_type,
    quantity,
    trigger_price,
    reduce_only=True,
    working_type=None,
):
    """
    Places a conditional order (STOP_MARKET / TAKE_PROFIT_MARKET / etc.)
    via Binance's /fapi/v1/algoOrder endpoint, which conditional orders
    were migrated to as of Dec 2025. Regular create_order() with these
    types no longer works.
    """
    params = {
        "symbol": exchange.market_id(symbol),
        "side": side.upper(),
        "type": order_type,  # "STOP_MARKET" or "TAKE_PROFIT_MARKET"
        "algoType": "CONDITIONAL",
        "quantity": quantity,
        "triggerPrice": trigger_price,  # note: triggerPrice, not stopPrice
        "reduceOnly": reduce_only,
    }
    if working_type:
        params["workingType"] = working_type  # "MARK_PRICE" or "CONTRACT_PRICE"

    logger.info(
        f"[algo_order] Placing {order_type} for {symbol}: "
        f"side={side}, qty={quantity}, trigger={trigger_price}, params={params}"
    )
    try:
        result = exchange.fapiprivate_post_algoorder(params)
    except Exception as e:
        logger.error(
            f"[algo_order] FAILED to place {order_type} for {symbol} "
            f"(side={side}, qty={quantity}, trigger={trigger_price}): {e}",
            exc_info=True,
        )
        raise

    # Normalize response so downstream code can treat it like a regular
    # ccxt order dict (algo orders return algoId, not id/orderId)
    result["id"] = result.get("algoId") or result.get("clientAlgoId")
    logger.info(
        f"[algo_order] Placed {order_type} for {symbol}: "
        f"algoId={result.get('algoId')}, algoStatus={result.get('algoStatus')}"
    )
    return result


def create_binance_future_order(
    side: str,
    symbol: str,
    user: User,
    *,
    sl: float | None = None,
    tp: float | None = None,
    tps: list | None = None,
    leverage: int = 5,
    position_pct: float = 10,
):
    try:
        margin_mode = "crossed"
        side = side.lower()
        position = position_pct
        position_direction = (
            FutureOrder.TradeDirection.LONG
            if side == "buy"
            else FutureOrder.TradeDirection.SHORT
        )

        allowed, reason = can_open_futures_position(user, symbol, position_direction)
        if not allowed:
            logger.info(
                f"[risk] Skipping futures order for {user.username} {symbol}: {reason}"
            )
            return False

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
        logger.info(f"order: {order}")

        inv_side = opposite_side(side)
        # Validate manual SL/TP against current price to avoid immediate triggers
        cur = float(current_price_of_symbol)
        if sl is not None:
            s = float(sl)
            if (side == "buy" and s >= cur) or (side == "sell" and s <= cur):
                logger.error(
                    f"[sl] Invalid SL for {symbol}: sl={s}, current={cur}, side={side}"
                )
                raise ValueError("Invalid SL relative to current price")
        if tp is not None:
            t = float(tp)
            if (side == "buy" and t <= cur) or (side == "sell" and t >= cur):
                logger.error(
                    f"[tp] Invalid single TP for {symbol}: tp={t}, current={cur}, side={side}"
                )
                raise ValueError("Invalid TP relative to current price")

        # Stop loss: optionally disabled via feature flag
        sl_order = None
        stop_price = None
        sl_trigger = None
        if settings.DISABLE_FUTURES_STOP_LOSS:
            logger.info(
                f"[sl] Stop loss disabled via feature flag for {symbol}, skipping."
            )
        else:
            stop_price = (
                float(sl)
                if sl is not None
                else compute_default_sl(order["average"], side)
            )
            sl_trigger = float(exchange.priceToPrecision(symbol, stop_price))
            try:
                sl_order = create_algo_order(
                    exchange,
                    symbol,
                    inv_side,
                    "STOP_MARKET",
                    quantity,
                    trigger_price=sl_trigger,
                    reduce_only=True,
                )
            except Exception as e:
                logger.error(
                    f"[sl] Error creating SL algo order for {symbol} "
                    f"(trigger={sl_trigger}, qty={quantity}): {e}",
                    exc_info=True,
                )
                sl_order = None

        if sl_order is None and not settings.DISABLE_FUTURES_STOP_LOSS:
            logger.warning(
                f"[sl] No SL order was created for {symbol}, user={user.username}. "
                f"Position is UNPROTECTED."
            )

        # Optional single TP or multiple TPs
        tp_order = None
        created_tps = []
        base_qty = float(quantity)
        if tps:
            logger.info(
                f"[tp] Processing {len(tps)} TP definitions for {symbol}: {tps}"
            )
            # tps expected as list of dicts: {"price": float, "percent": float}
            market_info = exchange.market(symbol)
            min_amount = float(
                (market_info.get("limits") or {}).get("amount", {}).get("min") or 0
            )
            min_cost = float(
                (market_info.get("limits") or {}).get("cost", {}).get("min") or 0
            )
            for idx, tp_def in enumerate(tps):
                try:
                    p = float(tp_def.get("price"))
                    pct = float(tp_def.get("percent"))
                except Exception as e:
                    logger.error(
                        f"[tp] TP #{idx} for {symbol} has invalid price/percent: {tp_def} ({e})"
                    )
                    continue
                if pct <= 0:
                    logger.warning(
                        f"[tp] TP #{idx} for {symbol} skipped: percent<=0 ({pct})"
                    )
                    continue
                # Validate direction
                if side == "buy" and p <= cur:
                    logger.error(
                        f"[tp] TP #{idx} for {symbol} invalid: price {p} <= current {cur} for long"
                    )
                    raise ValueError("TP must be above current for long")
                if side == "sell" and p >= cur:
                    logger.error(
                        f"[tp] TP #{idx} for {symbol} invalid: price {p} >= current {cur} for short"
                    )
                    raise ValueError("TP must be below current for short")
                part_qty = base_qty * (pct / 100.0)
                part_qty_p = float(exchange.amountToPrecision(symbol, part_qty))
                stop_p = float(exchange.priceToPrecision(symbol, p))
                if part_qty_p <= 0:
                    logger.warning(
                        f"[tp] TP #{idx} for {symbol} skipped: rounded qty is 0 "
                        f"(base_qty={base_qty}, pct={pct})"
                    )
                    continue
                if min_amount and part_qty_p < min_amount:
                    logger.warning(
                        f"[tp] TP #{idx} for {symbol} skipped: qty {part_qty_p} "
                        f"below exchange min_amount {min_amount}"
                    )
                    continue
                if min_cost and (part_qty_p * stop_p) < min_cost:
                    logger.warning(
                        f"[tp] TP #{idx} for {symbol} skipped: notional "
                        f"{part_qty_p * stop_p} below exchange min_cost {min_cost}"
                    )
                    continue
                try:
                    tp_o = create_algo_order(
                        exchange,
                        symbol,
                        inv_side,
                        "TAKE_PROFIT_MARKET",
                        part_qty_p,
                        trigger_price=stop_p,
                        reduce_only=True,
                    )
                    created_tps.append(
                        {
                            "id": tp_o.get("id", ""),
                            "price": stop_p,
                            "percent": pct,
                            "qty": part_qty_p,
                        }
                    )
                except Exception as e:
                    logger.error(
                        f"[tp] TP #{idx} for {symbol} FAILED to place "
                        f"(price={stop_p}, qty={part_qty_p}): {e}",
                        exc_info=True,
                    )
                    continue

            if len(created_tps) < len(
                [t for t in tps if float(t.get("percent") or 0) > 0]
            ):
                logger.warning(
                    f"[tp] {symbol}: only {len(created_tps)}/{len(tps)} TP orders "
                    f"were actually created. Check preceding log lines for skips/failures."
                )
        elif tp is not None:
            # Map single TP to a child TP covering 100%
            tp_price = float(exchange.priceToPrecision(symbol, float(tp)))
            try:
                tp_o = create_algo_order(
                    exchange,
                    symbol,
                    inv_side,
                    "TAKE_PROFIT_MARKET",
                    quantity,
                    trigger_price=tp_price,
                    reduce_only=True,
                )
                created_tps.append(
                    {
                        "id": tp_o.get("id", ""),
                        "price": tp_price,
                        "percent": 100.0,
                        "qty": float(quantity),
                    }
                )
            except Exception as e:
                logger.error(
                    f"[tp] Single TP for {symbol} FAILED to place "
                    f"(price={tp_price}, qty={quantity}): {e}",
                    exc_info=True,
                )

        if not created_tps:
            logger.warning(
                f"[tp] No TP orders were created for {symbol}, user={user.username}."
            )

        fee = order.get("fee") or {}
        entry_fee = fee.get("cost", 0)
        entry_fee_currency = fee.get("currency", "USDT")
        total_fee = fee.get("cost", 0)

        # Capture SL info when enabled; otherwise mark as cancelled with a unique placeholder id
        if sl_order:
            # Use sl_trigger (the price we actually sent to Binance) rather
            # than parsing it back out of the algoOrder ack: Binance echoes
            # a "price": "0" placeholder for these market-triggered stops,
            # and that truthy non-empty string was winning the `or` chain
            # here, silently persisting stop_loss_price=0.
            stop_loss_price = sl_trigger
            sl_id = sl_order["id"]
            sl_status = FutureOrder.TradeStatus.POSITION
        else:
            stop_loss_price = 0
            sl_id = f"DISABLED-{uuid4()}"
            sl_status = FutureOrder.TradeStatus.CANCELLED

        order = exchange.fetch_order(
            order["id"],
            symbol,
        )

        logger.info(f"order: {order}")

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
            stop_loss_order_id=sl_id,
            stop_loss_price=stop_loss_price,
            stop_loss_status=sl_status,
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

        logger.info(
            f"[summary] {symbol} order {fobj.order_id} created for {user.username}: "
            f"sl_status={sl_status}, sl_id={sl_id}, tps_created={len(created_tps)}"
        )

        return True

    except Exception as e:
        logger.error(
            f"Error creating futures order for {user.username}: {e}", exc_info=True
        )
        return False
