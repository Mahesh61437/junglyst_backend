import os
from pathlib import Path
from datetime import timedelta
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Environment Check
RAILWAY_ENV = config('RAILWAY_ENVIRONMENT_NAME', default='')
IS_PRODUCTION = RAILWAY_ENV == 'production'

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-8e692pepdm++i+^8&ejp#ozjyb8%r6&+-e8x4239o=tw1lz0g^')

# SECURITY WARNING: don't run with debug turned on in production!
# Debug is False in production unless explicitly set to True in env
DEBUG = config('DEBUG', default=not IS_PRODUCTION, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*', cast=lambda v: [s.strip() for s in v.split(',')])
if IS_PRODUCTION:
    ALLOWED_HOSTS += [
        '.railway.app',
        '.up.railway.app',
        '.junglyst.com',
    ]

CSRF_TRUSTED_ORIGINS = [
    "https://*.railway.app",
    "https://*.up.railway.app",
    "https://junglyst.com",
    "https://*.junglyst.com",
]
# Add dynamic hosts from ALLOWED_HOSTS if any
CSRF_TRUSTED_ORIGINS += [f"https://{host}" for host in ALLOWED_HOSTS if host != '*' and f"https://{host}" not in CSRF_TRUSTED_ORIGINS]

# Production Security Headers
if IS_PRODUCTION:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    
    # Handle SuspiciousOperation when behind a proxy
    USE_X_FORWARDED_HOST = True
    USE_X_FORWARDED_PORT = True

# Application definition
INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # 3rd Party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    
    # Local Apps
    'core',
    'cart',
    'orders',
    'shipping',
    'payments',
    'notifications',
    'sellers',
    'analytics',
    'django_celery_results',
    'django_celery_beat',
]

JAZZMIN_SETTINGS = {
    "site_title": "Junglyst Admin",
    "site_header": "Junglyst",
    "site_brand": "Junglyst Curator",
    "site_brand_link": "admin:index",
    "site_logo": None,
    "welcome_sign": "Welcome to the Junglyst Master Registry",
    "copyright": "Junglyst Botanical Sanctuary",
    "search_model": ["sellers.SellerProfile", "core.Product"],
    "user_avatar": None,
    "topmenu_links": [
        {"name": "Home",  "url": "admin:index", "permissions": ["auth.view_user"]},
        {"name": "View Site", "url": "http://localhost:5173", "new_window": True},
        {"model": "sellers.SellerProfile"},
    ],
    "show_sidebar": True,
    "navigation_expanded": True,
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.Group": "fas fa-users",
        "sellers.SellerProfile": "fas fa-store",
        "sellers.AllowedSeller": "fas fa-user-check",
        "core.Product": "fas fa-leaf",
        "core.Category": "fas fa-list",
        "orders.Order": "fas fa-shopping-cart",
    },
    "order_with_respect_to": ["sellers", "core", "orders", "auth"],
    "use_google_fonts_cdn": True,
    "show_ui_builder": True,
}

JAZZMIN_UI_CONFIG = {
    "theme": "flatly",
    "dark_mode_theme": None,
    "navbar": "navbar-dark",
    "sidebar": "sidebar-dark-primary",
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'junglyst_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'junglyst_backend.wsgi.application'

# Email Settings
# if DEBUG:
#     EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
# else:
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')

# Database
if config('DB_HOST', default=None):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST'),
            'PORT': config('DB_PORT', default='5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# Caching
REDIS_URL = config('REDIS_URL', default=None)
REDISHOST = config('REDISHOST', default=None)
REDISPORT = config('REDISPORT', default='6379')
REDISUSER = config('REDISUSER', default='')
REDIS_PASSWORD = config('REDIS_PASSWORD', default='')

if REDISHOST and not REDIS_URL:
    if REDISUSER and REDIS_PASSWORD:
        REDIS_URL = f"redis://{REDISUSER}:{REDIS_PASSWORD}@{REDISHOST}:{REDISPORT}/0"
    elif REDIS_PASSWORD:
        REDIS_URL = f"redis://:{REDIS_PASSWORD}@{REDISHOST}:{REDISPORT}/0"
    else:
        REDIS_URL = f"redis://{REDISHOST}:{REDISPORT}/0"

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            }
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "unique-snowflake",
        }
    }

# Custom User Model
AUTH_USER_MODEL = 'core.User'

AUTHENTICATION_BACKENDS = [
    'core.backends.EmailOrUsernameModelBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ),
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle'
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/day',
        'user': '1000/day',
        'auth': '10/minute',
    }
}

# JWT Configuration
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# CORS Configuration
if IS_PRODUCTION:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGIN_REGEXES = [
        r"^https://.*\.railway\.app$",
        r"^https://.*\.up\.railway\.app$",
        r"^https://junglyst\.com$",
        r"^https://.*\.junglyst\.com$",
        r"^http://localhost:5173$", # React default
        r"^http://localhost:3000$", # Alternative
    ]
else:
    CORS_ALLOW_ALL_ORIGINS = True

CORS_ALLOW_CREDENTIALS = True

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# Celery Configuration
CELERY_BROKER_URL = REDIS_URL if REDIS_URL else 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'django-db'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# Sample Periodic Tasks
CELERY_BEAT_SCHEDULE = {
    'sync-shipment-statuses': {
        'task': 'shipping.tasks.sync_all_shipment_statuses',
        'schedule': 3600.0,  # every hour
    },
}

# Static & Media Files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs/debug.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': config('DJANGO_LOG_LEVEL', default='INFO'),
            'propagate': True,
        },
        'core': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': True,
        },
    },
}

os.makedirs(BASE_DIR / 'logs', exist_ok=True)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Integration Credentials
RAZORPAY_KEY_ID = config('RAZORPAY_KEY_ID', default='')
RAZORPAY_KEY_SECRET = config('RAZORPAY_KEY_SECRET', default='')
ENABLE_PAYMENTS = config('ENABLE_PAYMENTS', default=False, cast=bool)

NIMBUSPOST_TOKEN = config('NIMBUSPOST_TOKEN', default='')
SHIPROCKET_EMAIL = config('SHIPROCKET_EMAIL', default='')
SHIPROCKET_PASSWORD = config('SHIPROCKET_PASSWORD', default='')

FIREBASE_CONFIG = {
    "apiKey": config('FIREBASE_API_KEY', default=''),
    "authDomain": config('FIREBASE_AUTH_DOMAIN', default=''),
    "projectId": config('FIREBASE_PROJECT_ID', default=''),
    "storageBucket": config('FIREBASE_STORAGE_BUCKET', default=''),
    "messagingSenderId": config('FIREBASE_MESSAGING_SENDER_ID', default=''),
    "appId": config('FIREBASE_APP_ID', default='')
}
