from apps.accounts.models import User, UserKey
from apps.trade.models import SpotOrder, SpotTakeProfit, TradeSettings
from apps.trade.utils.common import (
    make_spot_exchange,
    get_symbol_last_price,
    compute_default_sl,
)
from apps.trade.utils.risk_guard import can_open_spot_position
from apps.trade.utils.locks import user_trade_open_lock

import ccxt
import logging
from django.conf import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_binance_spot_order(
    side: str,
    symbol: str,
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
        # Per-user, UI-configurable in Risk Settings; an explicit arg (if
        # ever passed by a caller) still takes precedence over the saved
        # default.
        if position_pct is None:
            position_pct = float(TradeSettings.get_for_user(user).spot_position_pct)
        position = position_pct

        with user_trade_open_lock(user.id):
            allowed, reason = can_open_spot_position(user, symbol)
            if not allowed:
                logger.info(
                    f"[risk] Skipping spot order for {user.username} {symbol}: {reason}"
                )
                return False

            user_binance_key = UserKey.objects.get(user=user, is_active=True)
            exchange = make_spot_exchange(
                api_key=user_binance_key.api_key, api_secret=user_binance_key.api_secret
            )

            # Get market info for minimum order quantity check
            market = exchange.market(symbol)
            current_price_of_symbol = get_symbol_last_price(exchange, symbol)

            if not current_price_of_symbol:
                logger.error(f"Could not fetch current price for {symbol}")
                return False

            balance = exchange.fetch_balance()

            if side == "buy":
                # For buy orders, we use USDT balance
                user_balance = balance["free"]["USDT"]
                user_usable_balance = user_balance * position / 100
                quantity = user_usable_balance / current_price_of_symbol
            else:
                # For sell orders, we use the available crypto balance
                base_currency = symbol.split("/")[0]
                user_balance = balance["free"].get(base_currency, 0)
                quantity = user_balance * position / 100

            # Check minimum order quantity
            min_amount = float(market["limits"]["amount"]["min"])
            if quantity < min_amount:
                logger.error(
                    f"Order quantity {quantity} is below minimum {min_amount} for {symbol}"
                )
                return False

            # Check minimum notional value (price * quantity)
            min_notional = float(market["limits"]["cost"]["min"])
            notional_value = quantity * current_price_of_symbol
            if notional_value < min_notional:
                logger.error(
                    f"Order notional value {notional_value} is below minimum {min_notional} for {symbol}"
                )
                return False

            quantity = float(exchange.amountToPrecision(symbol, quantity))

            # Check if we have sufficient balance
            if quantity <= 0:
                logger.error(f"Insufficient balance for {symbol} {side} order")
                return False

            order = exchange.create_order(
                symbol=symbol, side=side, type="market", amount=quantity
            )

            # Extract detailed fee information
            fee_details = {
                "cost": order["fee"]["cost"],
                "currency": order["fee"]["currency"],
            }

            position_direction = (
                SpotOrder.TradeDirection.LONG
                if side == "buy"
                else SpotOrder.TradeDirection.SHORT
            )

            created = SpotOrder.objects.create(
                order_id=order["id"],
                entry_price=order["average"],
                direction=position_direction,
                order_quantity=quantity,
                final_quantity=float(quantity) - float(fee_details["cost"]),
                entry_fee=fee_details["cost"],
                entry_fee_currency=fee_details["currency"],
                total_fee=float(order["average"]) * fee_details["cost"],
                symbol=symbol,
                is_spot=True,
                total_cost=notional_value,
                exchange="binance",
                user=user,
            )

            cur = float(current_price_of_symbol)
            min_amount = float(market["limits"]["amount"]["min"])
            min_cost = float(market["limits"]["cost"]["min"])

            # Signals carrying tp/tps are limit-TP-only: place resting LIMIT
            # sell orders at each target and skip stop-loss entirely (no sl
            # field is sent for these, and we don't synthesize a default).
            # Legacy signals with neither tp nor tps keep the original
            # single protective stop-loss behavior below.
            if side == "buy" and (tps or tp is not None):
                base_qty = float(created.final_quantity or quantity)
                tp_defs = list(tps) if tps else [{"price": tp, "percent": 100.0}]

                created_tps = []
                for idx, tp_def in enumerate(tp_defs):
                    try:
                        p = float(tp_def.get("price"))
                        pct = float(tp_def.get("percent"))
                    except Exception as e:
                        logger.error(
                            f"[tp] Spot TP #{idx} for {symbol} has invalid price/percent: {tp_def} ({e})"
                        )
                        continue
                    if pct <= 0:
                        logger.warning(
                            f"[tp] Spot TP #{idx} for {symbol} skipped: percent<=0 ({pct})"
                        )
                        continue
                    if p <= cur:
                        logger.error(
                            f"[tp] Spot TP #{idx} for {symbol} invalid: price {p} <= current {cur}"
                        )
                        raise ValueError("TP must be above current price for a spot long")
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
                            tp_order_id=tp_o.get("id", ""),
                            price=price_p,
                            percent=pct,
                            quantity=qty_p,
                            status=SpotTakeProfit.TradeStatus.POSITION,
                        )
                        created_tps.append(tp_o)
                    except Exception as e:
                        logger.error(
                            f"[tp] Spot TP #{idx} for {symbol} FAILED to place "
                            f"(price={price_p}, qty={qty_p}): {e}",
                            exc_info=True,
                        )
                        continue

                if not created_tps:
                    logger.warning(
                        f"[tp] No spot TP orders were created for {symbol}, user={user.username}."
                    )

                try:
                    created.stop_loss_status = SpotOrder.TradeStatus.CANCELLED
                    created.save(update_fields=["stop_loss_status"])
                except Exception:
                    pass
            # Attempt to place a protective stop-loss order for spot, unless disabled
            elif not settings.DISABLE_SPOT_STOP_LOSS:
                try:
                    if side == "buy":
                        sl_side = "sell"
                        amount = float(created.final_quantity) or float(quantity)
                        sl_price = (
                            float(sl) if sl else compute_default_sl(order["average"], side)
                        )
                        # Binance spot typically uses STOP_LOSS_LIMIT; set price equal to stopPrice (tight limit)
                        params = {
                            "stopPrice": float(exchange.priceToPrecision(symbol, sl_price))
                        }
                        limit_price = params["stopPrice"]  # simple approximation
                        # Place protective stop as limit stop to increase acceptance on spot markets
                        sl_created = exchange.create_order(
                            symbol=symbol,
                            side=sl_side,
                            type="STOP_LOSS_LIMIT",
                            amount=float(exchange.amountToPrecision(symbol, amount)),
                            price=float(exchange.priceToPrecision(symbol, limit_price)),
                            params=params,
                        )
                        try:
                            created.stop_loss_order_id = sl_created.get("id", "")
                            created.stop_loss_price = params["stopPrice"]
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
                    logger.warning(f"Failed to place spot stop-loss for {symbol}: {e}")
            else:
                try:
                    created.stop_loss_status = SpotOrder.TradeStatus.CANCELLED
                    created.save(update_fields=["stop_loss_status"])
                except Exception:
                    pass

            # Optional DCA (average-down): a resting LIMIT buy below entry,
            # same quantity as the initial fill. When it fills, refresh_spot_order
            # recalculates order_quantity/entry_price and resizes open TPs.
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
                            created.dca_order_id = dca_o.get("id", "")
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
                f"Spot {side} order created for {user.username}: "
                f"{quantity} {symbol} at {order['average']}. "
                f"Fee: {fee_details['cost']} {fee_details['currency']}"
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
            f"Error creating spot order for {user.username}: {str(e)}", exc_info=True
        )
        return False
