from config import celery_app
from django.db import transaction

from apps.accounts.models import UserKey, User
from apps.trade.models import SpotOrder, FutureOrder
from apps.trade.utils.create_market_order import create_binance_future_order
from apps.trade.utils.close_order import quick_close_position

from apps.trade.utils.close_market_order_spot import quick_close_spot_position
from apps.trade.utils.create_market_binance_spot_order import create_binance_spot_order

import logging

logger = logging.getLogger(__name__)


@celery_app.task(bind=True)
def create_order_of_user_controller(self, side, symbol, market, sl=None, tp=None):
    try:
        users_key = UserKey.objects.filter(is_active=True)

        for user_key in users_key:
            create_order_of_user.delay(side, symbol, market, user_key.user.id, sl, tp)
    except Exception as e:
        print(f"Error dispatching  order create: {str(e)}")


@celery_app.task(
    bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3
)
def create_order_of_user(self, side, symbol, market, user_id, sl=None, tp=None):
    try:
        user = User.objects.get(id=user_id)
        if market == "futures":
            create_binance_future_order(side, symbol, user, sl=sl, tp=tp)
        else:
            create_binance_spot_order(side, symbol, user, sl=sl)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)


@celery_app.task(bind=True)
def close_order_of_user_controller(self, side, symbol, market):
    if market == "futures":
        position_direction = (
            FutureOrder.TradeDirection.LONG
            if side == "sell"
            else FutureOrder.TradeDirection.SHORT
        )
        print(position_direction)
        orders = FutureOrder.objects.filter(
            symbol=symbol,
            status=FutureOrder.TradeStatus.POSITION,
            direction=position_direction,
        )
        print(orders)
        for order in orders:
            quick_close_user_order.delay(order_id=order.id, market=market)
    else:
        position_direction = (
            SpotOrder.TradeDirection.LONG
            if side == "sell"
            else SpotOrder.TradeDirection.SHORT
        )
        print(position_direction)
        orders = SpotOrder.objects.filter(
            symbol=symbol,
            status=SpotOrder.TradeStatus.POSITION,
            direction=position_direction,
        )
        print(orders)
        for order in orders:
            quick_close_user_order.delay(order_id=order.id, market=market)


@celery_app.task(
    bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3
)
def quick_close_user_order(self, order_id, market):
    try:
        if market == "futures":
            order = FutureOrder.objects.get(id=order_id)
            quick_close_position(order=order, user=order.user)
        else:
            order = SpotOrder.objects.get(id=order_id)
            quick_close_spot_position(order=order, user=order.user)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)


# Futures signal orchestration respecting existing positions
@celery_app.task(bind=True)
def handle_futures_signal_controller(self, side, symbol, sl=None, tp=None):
    try:
        users_key = UserKey.objects.filter(is_active=True)
        for user_key in users_key:
            handle_futures_signal.delay(side, symbol, user_key.user.id, sl, tp)
    except Exception as e:
        logger.error(f"Error dispatching futures signal: {e}")


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def handle_futures_signal(self, side, symbol, user_id, sl=None, tp=None):
    try:
        user = User.objects.get(id=user_id)
        side = side.lower()
        # find any open positions for user+symbol
        open_orders = FutureOrder.objects.filter(
            user=user,
            symbol=symbol,
            status=FutureOrder.TradeStatus.POSITION,
        ).order_by("-created_at")

        if not open_orders.exists():
            # No open trade: open new
            create_binance_future_order(side, symbol, user, sl=sl, tp=tp)
            return

        existing = open_orders.first()
        if (side == "buy" and existing.direction == FutureOrder.TradeDirection.LONG) or (
            side == "sell" and existing.direction == FutureOrder.TradeDirection.SHORT
        ):
            # Same signal: ignore
            return

        # Opposite signal: close then open new
        quick_close_position(order=existing, user=user)
        create_binance_future_order(side, symbol, user, sl=sl, tp=tp)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)


# --- Legacy compatibility tasks -------------------------------------------------
# Some external producers may still dispatch tasks using old dotted paths.
# Register shims with those names so the worker does not error out.

@celery_app.task(name="apps.order.task.close_the_order_of_user_by_take_profit")
def compat_close_the_order_of_user_by_take_profit(trade_signal_id=None, *args, **kwargs):
    # We don't have enough context (user/symbol) to action this here.
    # Accept and no-op to avoid unregistered-task errors.
    logger.warning(
        "Received legacy task apps.order.task.close_the_order_of_user_by_take_profit; "
        f"trade_signal_id={trade_signal_id}. No-op."
    )
    return True
