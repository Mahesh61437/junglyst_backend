#!/bin/bash
set -e

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn server..."
# Use the wsgi module name we replaced earlier
exec gunicorn junglyst_backend.wsgi:application --bind 0.0.0.0:8000 --workers 3
