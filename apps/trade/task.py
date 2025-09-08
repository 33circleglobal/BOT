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
def create_order_of_user_controller(self, side, symbol, market):
    try:
        users_key = UserKey.objects.filter(is_active=True)

        for user_key in users_key:
            create_order_of_user.delay(side, symbol, market, user_key.user.id)
    except Exception as e:
        print(f"Error dispatching  order create: {str(e)}")


@celery_app.task(
    bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3
)
def create_order_of_user(self, side, symbol, market, user_id):
    try:
        user = User.objects.get(id=user_id)
        if market == "futures":
            create_binance_future_order(side, symbol, user)
        else:
            create_binance_spot_order(side, symbol, user)
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
