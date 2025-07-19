#!/bin/sh

# Apply database migrations
python manage.py migrate

# Add crontab tasks
python manage.py crontab add

# Start Celery worker in the background
# celery -A config worker -l info -P gevent -c 500 

gunicorn config.wsgi:application -b 0.0.0.0:80 --workers 3