from apps.accounts.models import User, UserHyperLiquidKey
from apps.trade.models import FutureOrder, FutureTakeProfit, TradeSettings
from apps.trade.utils.common import compute_default_sl, opposite_side
from apps.trade.utils.hyperliquid_common import (
    make_hyperliquid_exchange,
    to_hyperliquid_symbol,
    create_trigger_order,
    get_hyperliquid_last_price,
)
from apps.trade.utils.risk_guard import can_open_futures_position
from apps.trade.utils.locks import user_trade_open_lock

import logging
from django.conf import settings
from uuid import uuid4

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_hyperliquid_future_order(
    side: str,
    raw_symbol: str,
    user: User,
    *,
    sl: float | None = None,
    tp: float | None = None,
    tps: list | None = None,
    leverage: int | None = None,
    position_pct: float | None = None,
):
    try:
        side = side.lower()
        trade_settings = TradeSettings.get_for_user(user)
        if leverage is None:
            leverage = trade_settings.futures_leverage
        if position_pct is None:
            position_pct = float(trade_settings.futures_position_pct)
        position = position_pct
        position_direction = (
            FutureOrder.TradeDirection.LONG
            if side == "buy"
            else FutureOrder.TradeDirection.SHORT
        )

        with user_trade_open_lock(user.id):
            allowed, reason = can_open_futures_position(user, raw_symbol, position_direction)
            if not allowed:
                logger.info(
                    f"[risk] Skipping HyperLiquid futures order for {user.username} "
                    f"{raw_symbol}: {reason}"
                )
                return False

            hl_key = UserHyperLiquidKey.objects.get(user=user, is_active=True)
            exchange = make_hyperliquid_exchange(
                wallet_address=hl_key.get_master_wallet_address(),
                private_key=hl_key.get_api_private_key(),
            )
            symbol = to_hyperliquid_symbol(raw_symbol, "futures")

            if symbol not in exchange.markets:
                logger.warning(
                    f"[hl] No HyperLiquid perpetual market for {symbol} (derived from "
                    f"{raw_symbol}). Skipping futures {raw_symbol} order for "
                    f"{user.username}."
                )
                return False

            current_price_of_symbol = get_hyperliquid_last_price(exchange, symbol)
            if not current_price_of_symbol:
                logger.error(f"Could not fetch current price for {symbol}")
                return False

            balance = exchange.fetch_balance(params={"type": "swap"})
            user_balance = float((balance.get("free") or {}).get("USDC") or 0)

            user_usable_balance = (user_balance * position / 100) * leverage
            quantity = user_usable_balance / current_price_of_symbol
            quantity = exchange.amountToPrecision(symbol, quantity)

            exchange.set_leverage(leverage, symbol, params={"marginMode": "cross"})

            order = exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=quantity,
                price=current_price_of_symbol,
            )
            logger.info(f"order: {order}")

            inv_side = opposite_side(side)
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
                    else compute_default_sl(current_price_of_symbol, side)
                )
                sl_trigger = float(exchange.priceToPrecision(symbol, stop_price))
                try:
                    sl_order = create_trigger_order(
                        exchange,
                        symbol,
                        inv_side,
                        quantity,
                        sl_trigger,
                        take_profit=False,
                    )
                except Exception as e:
                    logger.error(
                        f"[sl] Error creating SL trigger order for {symbol} "
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
            created_tps = []
            base_qty = float(quantity)
            if tps:
                logger.info(
                    f"[tp] Processing {len(tps)} TP definitions for {symbol}: {tps}"
                )
                market_info = exchange.market(symbol)
                min_amount = float(
                    ((market_info.get("limits") or {}).get("amount") or {}).get("min") or 0
                )
                min_cost = float(
                    ((market_info.get("limits") or {}).get("cost") or {}).get("min") or 0
                )
                total_pct = sum(float(t.get("percent") or 0) for t in tps)
                covers_full_position = total_pct >= 99.9
                remaining_qty = base_qty
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
                    is_last = idx == len(tps) - 1
                    if is_last and covers_full_position:
                        part_qty = max(remaining_qty, 0)
                    else:
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
                        tp_o = create_trigger_order(
                            exchange,
                            symbol,
                            inv_side,
                            part_qty_p,
                            stop_p,
                            take_profit=True,
                        )
                        created_tps.append(
                            {
                                "id": tp_o.get("id", ""),
                                "price": stop_p,
                                "percent": pct,
                                "qty": part_qty_p,
                            }
                        )
                        remaining_qty -= part_qty_p
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
                tp_price = float(exchange.priceToPrecision(symbol, float(tp)))
                try:
                    tp_o = create_trigger_order(
                        exchange,
                        symbol,
                        inv_side,
                        quantity,
                        tp_price,
                        take_profit=True,
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

            # HyperLiquid's unified order response carries no fee breakdown
            # (unlike Binance) — fee tracking is left at zero here.
            entry_fee = 0
            entry_fee_currency = "USDC"
            total_fee = 0

            if sl_order:
                stop_loss_price = sl_trigger
                sl_id = sl_order["id"]
                sl_status = FutureOrder.TradeStatus.POSITION
            else:
                stop_loss_price = 0
                sl_id = f"DISABLED-{uuid4()}"
                sl_status = FutureOrder.TradeStatus.CANCELLED

            # Note: deliberately not re-fetching this order via fetch_order()
            # afterward (as the Binance path does) — HyperLiquid's orderStatus
            # endpoint reports no fill price once an order is queried after
            # the fact (only its own preset limit price), so re-fetching here
            # would silently replace the correct average from the create
            # response below with a wrong/misleading one.
            fobj = FutureOrder.objects.create(
                order_id=order["id"],
                symbol=raw_symbol,
                direction=position_direction,
                exchange=FutureOrder.ExchangeType.HYPERLIQUID,
                leverage=leverage,
                order_quantity=quantity,
                entry_price=order.get("average") or current_price_of_symbol,
                entry_fee=entry_fee,
                entry_fee_currency=entry_fee_currency,
                total_fee=total_fee,
                stop_loss_order_id=sl_id,
                stop_loss_price=stop_loss_price,
                stop_loss_status=sl_status,
                user=user,
            )

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
            f"Error creating HyperLiquid futures order for {user.username}: {e}",
            exc_info=True,
        )
        return False
