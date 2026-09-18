from apps.accounts.models import User, UserHyperLiquidKey
from apps.trade.models import SpotOrder, SpotTakeProfit, TradeSettings
from apps.trade.utils.common import compute_default_sl
from apps.trade.utils.hyperliquid_common import (
    make_hyperliquid_exchange,
    to_hyperliquid_symbol,
    create_trigger_order,
    get_hyperliquid_last_price,
)
from apps.trade.utils.risk_guard import can_open_spot_position
from apps.trade.utils.locks import user_trade_open_lock

import ccxt
import logging
from django.conf import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_hyperliquid_spot_order(
    side: str,
    raw_symbol: str,
    user: User,
    *,
    sl: float | None = None,
    tp: float | None = None,
    tps: list | None = None,
    dca: float | None = None,
    position_pct: float | None = None,
):
    try:
        side = side.lower()
        if position_pct is None:
            position_pct = float(TradeSettings.get_for_user(user).spot_position_pct)
        position = position_pct

        with user_trade_open_lock(user.id):
            allowed, reason = can_open_spot_position(user, raw_symbol)
            if not allowed:
                logger.info(
                    f"[risk] Skipping HyperLiquid spot order for {user.username} "
                    f"{raw_symbol}: {reason}"
                )
                return False

            hl_key = UserHyperLiquidKey.objects.get(user=user, is_active=True)
            exchange = make_hyperliquid_exchange(
                wallet_address=hl_key.get_master_wallet_address(),
                private_key=hl_key.get_api_private_key(),
            )
            symbol = to_hyperliquid_symbol(raw_symbol, "spot")

            if symbol not in exchange.markets:
                logger.warning(
                    f"[hl] No HyperLiquid spot market for {symbol} (derived from "
                    f"{raw_symbol}). HyperLiquid's spot listings are a separate, much "
                    f"smaller set than its perpetuals — mostly native/community tokens "
                    f"plus a handful of majors (BTC, ETH, SOL, ...) — so most coins that "
                    f"trade as a perp have no spot pair here. Skipping spot {raw_symbol} "
                    f"order for {user.username}."
                )
                return False

            market = exchange.market(symbol)
            current_price_of_symbol = get_hyperliquid_last_price(exchange, symbol)

            if not current_price_of_symbol:
                logger.error(f"Could not fetch current price for {symbol}")
                return False

            balance = exchange.fetch_balance(params={"type": "spot"})

            if side == "buy":
                user_balance = float((balance.get("free") or {}).get("USDC") or 0)
                user_usable_balance = user_balance * position / 100
                quantity = user_usable_balance / current_price_of_symbol
            else:
                base_currency = symbol.split("/")[0]
                user_balance = float((balance.get("free") or {}).get(base_currency) or 0)
                quantity = user_balance * position / 100

            min_amount = float(
                ((market.get("limits") or {}).get("amount") or {}).get("min") or 0
            )
            if quantity < min_amount:
                logger.error(
                    f"Order quantity {quantity} is below minimum {min_amount} for {symbol}"
                )
                return False

            min_notional = float(
                ((market.get("limits") or {}).get("cost") or {}).get("min") or 0
            )
            notional_value = quantity * current_price_of_symbol
            if notional_value < min_notional:
                logger.error(
                    f"Order notional value {notional_value} is below minimum "
                    f"{min_notional} for {symbol}"
                )
                return False

            quantity = float(exchange.amountToPrecision(symbol, quantity))

            if quantity <= 0:
                logger.error(f"Insufficient balance for {symbol} {side} order")
                return False

            order = exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=quantity,
                price=current_price_of_symbol,
            )

            # HyperLiquid's unified order response carries no fee breakdown
            # (unlike Binance) — fee tracking is left at zero here.
            entry_fee_cost = 0
            entry_fee_currency = "USDC"

            position_direction = (
                SpotOrder.TradeDirection.LONG
                if side == "buy"
                else SpotOrder.TradeDirection.SHORT
            )

            entry_price = float(order.get("average") or current_price_of_symbol)

            created = SpotOrder.objects.create(
                order_id=order["id"],
                entry_price=entry_price,
                direction=position_direction,
                order_quantity=quantity,
                final_quantity=quantity,
                entry_fee=entry_fee_cost,
                entry_fee_currency=entry_fee_currency,
                total_fee=0,
                symbol=raw_symbol,
                is_spot=True,
                total_cost=notional_value,
                exchange=SpotOrder.ExchangeType.HYPERLIQUID,
                user=user,
            )

            cur = float(current_price_of_symbol)
            min_amount = float(
                ((market.get("limits") or {}).get("amount") or {}).get("min") or 0
            )
            min_cost = float(
                ((market.get("limits") or {}).get("cost") or {}).get("min") or 0
            )

            # Signals carrying tp/tps are limit-TP-only: place resting LIMIT
            # sell orders at each target and skip stop-loss entirely — matches
            # the Binance spot convention (see create_binance_spot_order).
            if side == "buy" and (tps or tp is not None):
                base_qty = float(created.final_quantity or quantity)
                tp_defs = list(tps) if tps else [{"price": tp, "percent": 100.0}]

                created_tps = []
                # Each leg's quantity gets rounded independently via
                # amountToPrecision, so the legs can sum to slightly less
                # than base_qty — leaving a dust remainder with no TP sized
                # to close it, or the declared percents might simply not
                # add up to 100% in the first place. Give the last leg
                # whatever's actually left instead of its own percent
                # share, unconditionally, so the position always fully
                # closes via TPs no matter what the percents were.
                remaining_qty = base_qty
                for idx, tp_def in enumerate(tp_defs):
                    try:
                        p = float(tp_def.get("price"))
                        pct = float(tp_def.get("percent"))
                    except Exception as e:
                        logger.error(
                            f"[tp] Spot TP #{idx} for {symbol} has invalid price/percent: "
                            f"{tp_def} ({e})"
                        )
                        continue
                    if pct <= 0:
                        logger.warning(
                            f"[tp] Spot TP #{idx} for {symbol} skipped: percent<=0 ({pct})"
                        )
                        continue
                    if p <= cur:
                        # Skip just this leg rather than raise — the order
                        # row is already persisted above, and an uncaught
                        # exception here would abort the remaining TPs plus
                        # the SL/DCA placement below for an otherwise-fine
                        # position.
                        logger.error(
                            f"[tp] Spot TP #{idx} for {symbol} invalid: price {p} <= current {cur}. "
                            f"Skipping this TP."
                        )
                        continue
                    is_last = idx == len(tp_defs) - 1
                    if is_last:
                        part_qty = max(remaining_qty, 0)
                    else:
                        part_qty = base_qty * (pct / 100.0)
                    qty_p = float(exchange.amountToPrecision(symbol, part_qty))
                    price_p = float(exchange.priceToPrecision(symbol, p))
                    if qty_p <= 0:
                        logger.warning(
                            f"[tp] Spot TP #{idx} for {symbol} skipped: rounded qty is 0"
                        )
                        continue
                    if min_amount and qty_p < min_amount:
                        logger.warning(
                            f"[tp] Spot TP #{idx} for {symbol} skipped: qty {qty_p} "
                            f"below exchange min_amount {min_amount}"
                        )
                        continue
                    if min_cost and (qty_p * price_p) < min_cost:
                        logger.warning(
                            f"[tp] Spot TP #{idx} for {symbol} skipped: notional "
                            f"{qty_p * price_p} below exchange min_cost {min_cost}"
                        )
                        continue
                    try:
                        tp_o = exchange.create_order(
                            symbol=symbol,
                            side="sell",
                            type="limit",
                            amount=qty_p,
                            price=price_p,
                        )
                        SpotTakeProfit.objects.create(
                            order=created,
                            tp_order_id=str(tp_o.get("id", "")),
                            price=price_p,
                            percent=pct,
                            quantity=qty_p,
                            status=SpotTakeProfit.TradeStatus.POSITION,
                        )
                        created_tps.append(tp_o)
                        remaining_qty -= qty_p
                    except Exception as e:
                        logger.error(
                            f"[tp] Spot TP #{idx} for {symbol} FAILED to place "
                            f"(price={price_p}, qty={qty_p}): {e}",
                            exc_info=True,
                        )
                        continue

                if not created_tps:
                    logger.warning(
                        f"[tp] No spot TP orders were created for {symbol}, "
                        f"user={user.username}."
                    )

                try:
                    created.stop_loss_status = SpotOrder.TradeStatus.CANCELLED
                    created.save(update_fields=["stop_loss_status"])
                except Exception:
                    pass
            elif not settings.DISABLE_SPOT_STOP_LOSS:
                try:
                    if side == "buy":
                        sl_side = "sell"
                        amount = float(created.final_quantity) or float(quantity)
                        sl_price = (
                            float(sl) if sl else compute_default_sl(entry_price, side)
                        )
                        sl_created = create_trigger_order(
                            exchange,
                            symbol,
                            sl_side,
                            amount,
                            sl_price,
                            take_profit=False,
                        )
                        try:
                            created.stop_loss_order_id = sl_created.get("id", "")
                            created.stop_loss_price = float(
                                exchange.priceToPrecision(symbol, sl_price)
                            )
                            created.stop_loss_status = SpotOrder.TradeStatus.POSITION
                            created.save(
                                update_fields=[
                                    "stop_loss_order_id",
                                    "stop_loss_price",
                                    "stop_loss_status",
                                ]
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(
                        f"Failed to place HyperLiquid spot stop-loss for {symbol}: {e}"
                    )
            else:
                try:
                    created.stop_loss_status = SpotOrder.TradeStatus.CANCELLED
                    created.save(update_fields=["stop_loss_status"])
                except Exception:
                    pass

            # Optional DCA (average-down): a resting LIMIT buy below entry,
            # same quantity as the initial fill.
            if side == "buy" and dca is not None:
                try:
                    dca_price_val = float(dca)
                    if dca_price_val >= cur:
                        logger.error(
                            f"[dca] Invalid DCA price for {symbol}: dca={dca_price_val}, "
                            f"current={cur} (must be below current price for a spot long)"
                        )
                    else:
                        dca_qty_p = float(exchange.amountToPrecision(symbol, quantity))
                        dca_price_p = float(exchange.priceToPrecision(symbol, dca_price_val))
                        if min_amount and dca_qty_p < min_amount:
                            logger.warning(
                                f"[dca] DCA order for {symbol} skipped: qty {dca_qty_p} "
                                f"below exchange min_amount {min_amount}"
                            )
                        elif min_cost and (dca_qty_p * dca_price_p) < min_cost:
                            logger.warning(
                                f"[dca] DCA order for {symbol} skipped: notional "
                                f"{dca_qty_p * dca_price_p} below exchange min_cost {min_cost}"
                            )
                        else:
                            dca_o = exchange.create_order(
                                symbol=symbol,
                                side="buy",
                                type="limit",
                                amount=dca_qty_p,
                                price=dca_price_p,
                            )
                            created.dca_order_id = str(dca_o.get("id", ""))
                            created.dca_price = dca_price_p
                            created.dca_quantity = dca_qty_p
                            created.dca_status = SpotOrder.TradeStatus.POSITION
                            created.save(
                                update_fields=[
                                    "dca_order_id",
                                    "dca_price",
                                    "dca_quantity",
                                    "dca_status",
                                ]
                            )
                except Exception as e:
                    logger.error(
                        f"[dca] Failed to place DCA order for {symbol}: {e}",
                        exc_info=True,
                    )

            logger.info(
                f"HyperLiquid spot {side} order created for {user.username}: "
                f"{quantity} {symbol} at {entry_price}."
            )
            return True

    except ccxt.InsufficientFunds as e:
        logger.error(f"Insufficient funds for {user.username}: {str(e)}")
        return False
    except ccxt.InvalidOrder as e:
        logger.error(f"Invalid order parameters for {user.username}: {str(e)}")
        return False
    except Exception as e:
        logger.error(
            f"Error creating HyperLiquid spot order for {user.username}: {str(e)}",
            exc_info=True,
        )
        return False
