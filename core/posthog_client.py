"""
Lazily-initialised PostHog client shared across the backend.

PostHog is used here purely for *operational* telemetry — API performance and
error metrics (see ``core.middleware.PostHogAPIMetricsMiddleware``). It is a
no-op unless ``POSTHOG_API_KEY`` is configured, so local/dev and test runs send
nothing.

The Python SDK batches events and flushes them on a background thread, so
``capture()`` does not add latency to the request/response cycle.
"""
from django.conf import settings

_client = None
_initialised = False


def get_posthog():
    """Return a configured Posthog client, or ``None`` when disabled."""
    global _client, _initialised
    if _initialised:
        return _client

    _initialised = True
    api_key = getattr(settings, 'POSTHOG_API_KEY', '') or ''
    if not api_key:
        _client = None
        return None

    try:
        from posthog import Posthog
        _client = Posthog(
            api_key,
            host=getattr(settings, 'POSTHOG_HOST', 'https://app.posthog.com'),
            # Operational metrics don't need person profiles — keeps cost down.
            disable_geoip=True,
        )
    except Exception:
        # Never let telemetry setup break the app.
        _client = None
    return _client


import logging
import traceback


class PostHogLoggingHandler(logging.Handler):
    """Forward log records to PostHog so every ``logger.warning/error/exception``
    — plus Django's own request/500 tracebacks — is captured centrally.

    Wired via ``settings.LOGGING`` at ``POSTHOG_LOG_LEVEL`` (default WARNING).
    A no-op unless ``POSTHOG_API_KEY`` is set. Records from PostHog's own
    transport stack are skipped so a failed flush can't recurse.
    """

    # Skip the SDK + its HTTP deps to avoid feedback loops when delivery fails.
    _SKIP_PREFIXES = ('posthog', 'urllib3', 'backoff', 'httpx', 'httpcore', 'requests')

    def emit(self, record):
        try:
            if record.name.startswith(self._SKIP_PREFIXES):
                return
            client = get_posthog()
            if client is None:
                return

            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)

            is_error = record.levelno >= logging.ERROR or bool(record.exc_info)
            properties = {
                'source': 'server',
                'level': record.levelname,
                'logger': record.name,
                'message': message[:2000],
                'module': record.module,
                'func': record.funcName,
                'line': record.lineno,
                'path': record.pathname,
                'event_kind': 'error' if is_error else 'log',
                '$process_person_profile': False,
            }

            if record.exc_info:
                exc_type, exc_val, exc_tb = record.exc_info
                properties['error_type'] = getattr(exc_type, '__name__', str(exc_type))
                properties['error'] = str(exc_val)[:1000]
                # Keep the tail — the deepest frames are the most useful.
                properties['traceback'] = ''.join(
                    traceback.format_exception(exc_type, exc_val, exc_tb)
                )[-5000:]

            client.capture(
                distinct_id='server',
                event='backend_log',
                properties=properties,
            )
        except Exception:
            # Logging handlers must never raise.
            pass
