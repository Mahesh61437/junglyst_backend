import os
from pathlib import Path
from datetime import timedelta
from decouple import config, Csv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Environment Check
RAILWAY_ENV = config('RAILWAY_ENVIRONMENT_NAME', default='')
IS_PRODUCTION = RAILWAY_ENV == 'production'

# SECURITY WARNING: keep the secret key used in production secret!
# In production we REFUSE to boot with the insecure default — a misconfigured
# deploy that falls back to a hardcoded key would let anyone forge JWTs and
# session cookies. Better to crash loudly than to ship a broken-by-default key.
_DEV_SECRET_KEY_FALLBACK = 'django-insecure-8e692pepdm++i+^8&ejp#ozjyb8%r6&+-e8x4239o=tw1lz0g^'
SECRET_KEY = config('SECRET_KEY', default=_DEV_SECRET_KEY_FALLBACK)
if IS_PRODUCTION and (SECRET_KEY == _DEV_SECRET_KEY_FALLBACK or SECRET_KEY.startswith('django-insecure-')):
    raise RuntimeError(
        "SECRET_KEY env var is not set (or still using the insecure dev default) in production. "
        "Set a strong random value in Railway env vars before deploying."
    )

# SECURITY WARNING: don't run with debug turned on in production!
# DEBUG is FORCED False in production — env override is ignored on purpose so a
# stray DEBUG=True in Railway can never dump settings/secrets to error pages.
DEBUG = False if IS_PRODUCTION else config('DEBUG', default=True, cast=bool)

# ALLOWED_HOSTS — production has a strict allow-list; '*' is rejected.
# Outside production, '*' is fine for local docker/dev.
if IS_PRODUCTION:
    _hosts = config('ALLOWED_HOSTS', default='', cast=lambda v: [s.strip() for s in v.split(',') if s.strip()])
    ALLOWED_HOSTS = list({h for h in _hosts if h != '*'}) + [
        '.railway.app',
        '.up.railway.app',
        '.junglyst.com',
        'junglyst.com',
    ]
else:
    ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*', cast=lambda v: [s.strip() for s in v.split(',')])

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
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_HTTPONLY = False  # frontend reads csrftoken cookie for non-JWT POSTs
    CSRF_COOKIE_SAMESITE = 'Lax'
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
    SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'  # disallow iframing the site (clickjacking)

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
    'anymail',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    'drf_spectacular',
    
    # Local Apps
    'core',
    'cart',
    'orders',
    'shipping',
    'payments',
    'notifications',
    'sellers',
    'analytics',
    'competition',
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

# Email
RESEND_API_KEY = config('RESEND_API_KEY', default='')
_default_email_backend = (
    'anymail.backends.resend.EmailBackend'
    if RESEND_API_KEY
    else 'django.core.mail.backends.console.EmailBackend'
)
# Allow override via env (e.g. SMTP for local mailpit)
EMAIL_BACKEND = config('EMAIL_BACKEND', default=_default_email_backend)
EMAIL_HOST = config('EMAIL_HOST', default='localhost')
EMAIL_PORT = config('EMAIL_PORT', default=25, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=False, cast=bool)
ANYMAIL = {
    'RESEND_API_KEY': RESEND_API_KEY,
}
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Junglyst <orders@junglyst.com>')

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

# Password policy — minimum 10 chars, blocks the most common passwords and
# values too similar to username/email. Higher than Django's default 8.
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 10},
    },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Default hasher remains PBKDF2 (Django default, FIPS-friendly).
# Argon2 listed first if/when `argon2-cffi` ships in requirements.
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
    'django.contrib.auth.hashers.BCryptSHA256PasswordHasher',
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
    'DEFAULT_PAGINATION_CLASS': 'core.pagination.StandardResultsSetPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle'
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '200/minute',   # stops scrapers/bots; casual browsing is ~5-15 req/page
        'user': '600/minute',   # authenticated users won't hit this under normal use
        'auth': '10/minute',    # keep tight for brute-force protection
    },
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Junglyst API',
    'DESCRIPTION': (
        'REST API for the Junglyst botanical marketplace — plants, aquatic specimens, '
        'and accessories. Covers buyer flows (auth, browsing, cart, checkout, orders) '
        'and seller flows (onboarding, product management, fulfilment).'
    ),
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SERVERS': [
        {'url': 'http://127.0.0.1:8000', 'description': 'Local development'},
        {'url': 'https://api.junglyst.com', 'description': 'Production'},
    ],
    'TAGS': [
        {'name': 'Auth', 'description': 'Registration, login, JWT refresh, password reset, current user.'},
        {'name': 'Products', 'description': 'Public product catalog, detail, search, reviews.'},
        {'name': 'Categories', 'description': 'Category and subcategory listings.'},
        {'name': 'Cart', 'description': 'Guest + authenticated cart management.'},
        {'name': 'Wishlist', 'description': 'Saved products per user.'},
        {'name': 'Checkout', 'description': 'Checkout, payment initiation, payment verification.'},
        {'name': 'Orders', 'description': 'Buyer order list, detail, tracking, cancellation.'},
        {'name': 'Shipping', 'description': 'Saved addresses, pincode serviceability, rate quotes.'},
        {'name': 'Sellers (public)', 'description': 'Public seller storefronts and listings.'},
        {'name': 'Sellers (dashboard)', 'description': 'Authenticated seller management endpoints.'},
        {'name': 'Seller Orders', 'description': 'Sub-order fulfilment workflow for sellers.'},
        {'name': 'Notifications', 'description': 'In-app notifications, newsletter, contact form.'},
        {'name': 'Misc', 'description': 'Home aggregate, config, bug reports, competition.'},
    ],
    'COMPONENT_SPLIT_REQUEST': True,
    'SORT_OPERATIONS': False,
    'SWAGGER_UI_SETTINGS': {
        'persistAuthorization': True,
        'displayRequestDuration': True,
    },
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

# Periodic Tasks
CELERY_BEAT_SCHEDULE = {
    'sync-shipment-statuses': {
        'task': 'shipping.tasks.sync_all_shipment_statuses',
        'schedule': 3600.0,  # every hour
    },
    # Payment reconciliation is on-demand (not periodic).
    # 4 delayed checks are scheduled per-payment at checkout time.
    # See payments.tasks.schedule_payment_checks()
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
        'shipping': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'sellers': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'orders': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

os.makedirs(BASE_DIR / 'logs', exist_ok=True)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Integration Credentials
CASHFREE_APP_ID = config('CASHFREE_APP_ID', default='')
CASHFREE_SECRET_KEY = config('CASHFREE_SECRET_KEY', default='')
CASHFREE_ENVIRONMENT = config('CASHFREE_ENVIRONMENT', default='SANDBOX')
ENABLE_PAYMENTS = config('ENABLE_PAYMENTS', default=False, cast=bool)

RAZORPAY_KEY_ID = config('RAZORPAY_KEY_ID', default='')
RAZORPAY_KEY_SECRET = config('RAZORPAY_KEY_SECRET', default='')
# Webhook secret is configured in the Razorpay dashboard when you create the
# webhook subscription — DISTINCT from RAZORPAY_KEY_SECRET. Used only to
# verify the X-Razorpay-Signature header on incoming webhook POSTs.
RAZORPAY_WEBHOOK_SECRET = config('RAZORPAY_WEBHOOK_SECRET', default='')

FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:5173')

NIMBUSPOST_EMAIL = config('NIMBUSPOST_EMAIL', default='')
NIMBUSPOST_PASSWORD = config('NIMBUSPOST_PASSWORD', default='')
NIMBUSPOST_WAREHOUSE_NAME = config('NIMBUSPOST_WAREHOUSE_NAME', default='Junglyst')
SHIPROCKET_EMAIL = config('SHIPROCKET_EMAIL', default='')
SHIPROCKET_PASSWORD = config('SHIPROCKET_PASSWORD', default='')
# Must match exactly the pickup_location name registered in your Shiprocket account
# (Shiprocket → Settings → Manage Pickup Addresses)
SHIPROCKET_PICKUP_LOCATION = config('SHIPROCKET_PICKUP_LOCATION', default='Mahesh')

FIREBASE_STORAGE_BUCKET = config('FIREBASE_STORAGE_BUCKET', default='')
FIREBASE_SERVICE_ACCOUNT_JSON = config('FIREBASE_SERVICE_ACCOUNT_JSON', default='')

# Production readiness check — fail fast at boot if any required env var is
# missing. Better to crash on deploy than to discover at checkout time.
if IS_PRODUCTION:
    _required = {
        'SECRET_KEY': SECRET_KEY,
        'DB_HOST': config('DB_HOST', default=''),
        'DB_NAME': config('DB_NAME', default=''),
        'DB_USER': config('DB_USER', default=''),
        'DB_PASSWORD': config('DB_PASSWORD', default=''),
    }
    _missing = [k for k, v in _required.items() if not v]
    if _missing:
        raise RuntimeError(
            f"Missing required production env vars: {', '.join(_missing)}. "
            f"Set them in Railway before deploying."
        )
    # Soft-warn (don't crash) if payment/shipping creds are absent — the site
    # can still serve catalog pages, but checkout will fail.
    import logging as _logging
    _log = _logging.getLogger(__name__)
    for _name in ('RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET', 'CASHFREE_APP_ID', 'CASHFREE_SECRET_KEY'):
        if not config(_name, default=''):
            _log.warning("Production startup: %s is not set — checkout will fail for that gateway.", _name)
    if not config('RAZORPAY_WEBHOOK_SECRET', default=''):
        _log.warning("Production startup: RAZORPAY_WEBHOOK_SECRET is not set — "
                     "Razorpay webhooks will be rejected as invalid signatures.")
