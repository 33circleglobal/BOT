from config import celery_app
from django.db import transaction

from apps.accounts.models import UserKey, User
from apps.trade.models import Order
from apps.trade.utils.create_market_order import create_binance_future_order
from apps.trade.utils.close_order import quick_close_position

from apps.trade.utils.close_market_order_spot import quick_close_spot_position
from apps.trade.utils.create_market_binance_spot_order import create_binance_spot_order

import logging

logger = logging.getLogger(__name__)


@celery_app.task(bind=True)
def create_order_of_user_controller(self, side, symbol):
    try:
        users_key = UserKey.objects.filter(is_active=True)

        for user_key in users_key:
            create_order_of_user.delay(side, symbol, user_key.user.id)
    except Exception as e:
        print(f"Error dispatching  order create: {str(e)}")


@celery_app.task(
    bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3
)
def create_order_of_user(self, side, symbol, user_id):
    try:
        user = User.objects.get(id=user_id)
        # create_binance_future_order(side, symbol, user)
        create_binance_spot_order(side, symbol, user)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)


@celery_app.task(bind=True)
def close_order_of_user_controller(self, side, symbol):
    position_direction = (
        Order.TradeDirection.LONG if side == "sell" else Order.TradeDirection.SHORT
    )
    print(position_direction)
    orders = Order.objects.filter(
        symbol=symbol, status=Order.TradeStatus.POSITION, direction=position_direction
    )
    print(orders)
    for order in orders:
        quick_close_user_order.delay(order_id=order.id)


@celery_app.task(
    bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3
)
def quick_close_user_order(self, order_id):
    try:
        order = Order.objects.get(id=order_id)
        # quick_close_position(order=order, user=order.user)
        quick_close_spot_position(order=order, user=order.user)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)
