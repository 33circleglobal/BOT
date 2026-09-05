import os
from celery import Celery
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()

app.conf.update(
    broker_url=settings.CELERY_BROKER_URL,
    broker_connection_retry_on_startup=True,
)

app.conf.beat_schedule = {
    "refresh-futures-positions-every-5-min": {
        "task": "apps.trade.task.refresh_all_futures_positions",
        "schedule": 300.0,
    },
}
