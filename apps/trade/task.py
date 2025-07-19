from celery import shared_task
from django.db import transaction

from apps.accounts.models import UserKey, User
from apps.trade.models import Order
from apps.trade.utils.create_market_order import create_binance_order
from apps.trade.utils.close_order import quick_close_position

import logging

logger = logging.getLogger(__name__)


@shared_task
def create_order_of_user_controller(side, symbol):
    try:
        users_key = UserKey.objects.filter(is_active=True)

        for user_key in users_key:
            create_order_of_user.delay(side, symbol, user_key.user.id)
    except Exception as e:
        print(f"Error dispatching  order create: {str(e)}")


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def create_order_of_user(self, side, symbol, user_id):
    try:
        user = User.objects.get(id=user_id)
        create_binance_order(side, symbol, user)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)


@shared_task
def close_order_of_user_controller(side, symbol):
    position_direction = (
        Order.TradeDirection.LONG if side == "buy" else Order.TradeDirection.SHORT
    )
    orders = Order.objects.filter(
        symbol=symbol, status=Order.TradeStatus.POSITION, direction=position_direction
    )
    for order in orders:
        quick_close_user_order.delay(order_id=order.id)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def quick_close_user_order(self, order_id):
    try:
        order = Order.objects.get(id=order_id)
        quick_close_position(order=order, user=order.user)
    except Exception as e:
        print("Caught exception:", e)
        raise self.retry(exc=e)
