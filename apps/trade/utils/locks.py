from contextlib import contextmanager

from django.core.cache import cache
from django.db import connection, transaction

# Arbitrary namespace so this doesn't collide with any other advisory locks.
_TRADE_OPEN_LOCK_NAMESPACE = 911001


@contextmanager
def user_trade_open_lock(user_id):
    """Serializes trade-opening for a single user.

    Concurrent webhook signals fan out into separate Celery tasks. Without
    this lock, N tasks can each read the same "open positions" count before
    any of them commits its new row, so all N pass the max-positions check
    and the cap gets exceeded (e.g. 3 signals landing at once on a 6/7 cap
    produced 9 open trades). Holding a Postgres advisory lock for the
    duration of the check + exchange calls + row creation makes those steps
    atomic per-user, so each task sees the previous one's result before
    deciding whether a new position is allowed.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [_TRADE_OPEN_LOCK_NAMESPACE, user_id],
            )
        yield


@contextmanager
def pending_order_lock(order_id):
    """Yields True to only one caller at a time per resting futures limit
    order. The 1-minute task, the 5-minute task and a manual refresh can all
    see the same fill; without this each would place its own SL/TPs. A cache
    lock rather than a row lock, so nothing already saved gets rolled back."""
    key = f"lock:pending-futures-order:{order_id}"
    acquired = cache.add(key, 1, timeout=300)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)
