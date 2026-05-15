release: python manage.py migrate
web: gunicorn junglyst_backend.wsgi
worker: celery -A junglyst_backend worker --loglevel=info
