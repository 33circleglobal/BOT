import os
from celery import Celery
from config import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()

app.conf.update(
    broker_url=settings.CELERY_BROKER_URL,
    broker_connection_retry_on_startup=True,
)
