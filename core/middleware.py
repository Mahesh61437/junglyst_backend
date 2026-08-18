"""
Operational middleware.

``PostHogAPIMetricsMiddleware`` records one PostHog event per API request with
server-side latency, status code, and error context — for performance and
debugging analysis. It is a no-op unless ``POSTHOG_API_KEY`` is set.

On any error response (status >= 400 or an unhandled exception) it also captures
the diagnostic context needed to understand *what went wrong*:
  * the request payload that was sent (JSON / form fields, sanitized),
  * the query string,
  * the error response body (e.g. DRF's "this field is required" detail).
Sensitive keys (passwords, tokens, card/cvv, signatures, auth) are redacted, and
file uploads are never read.

Cost control (the platform is budget-conscious):
  * Only ``/api/`` requests are tracked (static assets, admin, health are skipped).
  * Every error (status >= 400) and slow request (>= POSTHOG_API_SLOW_MS) is
    always captured; fast successful requests are sampled at
    POSTHOG_API_SAMPLE_RATE.
  * Routes are reported by their URL pattern (e.g. ``sellers/store/<slug>/``)
    rather than the raw path, so high-cardinality ids don't explode event
    properties.
"""
import json
import random
import time

from django.conf import settings

from .posthog_client import get_posthog

# Redact any key whose name contains one of these (case-insensitive).
_SENSITIVE_KEYS = (
    'password', 'token', 'secret', 'authorization', 'auth', 'card', 'cvv', 'cvc',
    'otp', 'pin', 'signature', 'access', 'refresh', 'api_key', 'apikey', 'key',
)
_MAX_BODY_BYTES = 8192       # don't snapshot request bodies larger than this
_MAX_STR = 500               # cap individual string values
_MAX_RESPONSE_CHARS = 2000   # cap the error response body


def _scrub(value, depth=0):
    """Recursively redact sensitive keys and truncate large values."""
    if depth > 6:
        return '…'
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(s in str(k).lower() for s in _SENSITIVE_KEYS):
                out[k] = '[REDACTED]'
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return value[:_MAX_STR]
    return value


class PostHogAPIMetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = bool(getattr(settings, 'POSTHOG_API_KEY', ''))
        self.sample_rate = float(getattr(settings, 'POSTHOG_API_SAMPLE_RATE', 1.0))
        self.slow_ms = float(getattr(settings, 'POSTHOG_API_SLOW_MS', 1000))
        self.prefix = getattr(settings, 'POSTHOG_API_PATH_PREFIX', '/api/')

    def __call__(self, request):
        if not self.enabled or not request.path.startswith(self.prefix):
            return self.get_response(request)

        # Snapshot the request payload up front — the body stream can only be read
        # once, and on error we want to know exactly what was sent.
        payload = self._snapshot_request(request)

        start = time.perf_counter()
        exception = None
        response = None
        try:
            response = self.get_response(request)
        except Exception as exc:  # re-raised after we record it
            exception = exc
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            status_code = getattr(response, 'status_code', 500)
            try:
                self._capture(request, status_code, duration_ms, exception, payload, response)
            except Exception:
                pass  # telemetry must never break the response

        return response

    # ── request/response introspection ───────────────────────────────────────

    def _snapshot_request(self, request):
        """Capture the request body cheaply and safely. Returns a dict describing
        the payload, or None. Never reads file uploads or oversized bodies."""
        try:
            ctype = (request.content_type or '').lower()
            if 'multipart/form-data' in ctype:
                return {'content_type': ctype, 'note': 'multipart form (files omitted)'}
            try:
                clen = int(request.META.get('CONTENT_LENGTH') or 0)
            except (TypeError, ValueError):
                clen = 0
            if clen > _MAX_BODY_BYTES:
                return {'content_type': ctype, 'note': f'body too large ({clen} bytes)'}
            body = request.body  # cached by Django; DRF can still re-read for JSON
            if not body:
                return None
            return {'content_type': ctype, 'raw': body[:_MAX_BODY_BYTES].decode('utf-8', errors='replace')}
        except Exception:
            return None

    def _payload_props(self, payload):
        """Turn a snapshot into a scrubbed, PostHog-friendly value."""
        if not payload:
            return None
        if 'note' in payload:
            return payload['note']
        raw = payload.get('raw')
        if not raw:
            return None
        ctype = payload.get('content_type', '')
        if 'application/json' in ctype:
            try:
                return _scrub(json.loads(raw))
            except Exception:
                pass
        if 'application/x-www-form-urlencoded' in ctype:
            try:
                from urllib.parse import parse_qs
                return _scrub({k: v if len(v) > 1 else v[0] for k, v in parse_qs(raw).items()})
            except Exception:
                pass
        return raw[:_MAX_RESPONSE_CHARS]

    def _response_body(self, response):
        """The error detail the API returned (e.g. DRF validation errors)."""
        try:
            # Streaming responses have no .content — skip them.
            if response is None or getattr(response, 'streaming', False):
                return None
            content = getattr(response, 'content', b'')
            if not content:
                return None
            text = content.decode('utf-8', errors='replace')
            try:
                return _scrub(json.loads(text))
            except Exception:
                return text[:_MAX_RESPONSE_CHARS]
        except Exception:
            return None

    def _route(self, request):
        match = getattr(request, 'resolver_match', None)
        if match is not None:
            return getattr(match, 'route', None) or match.view_name or request.path
        return request.path

    def _distinct_id(self, request):
        user = getattr(request, 'user', None)
        if getattr(user, 'is_authenticated', False):
            return str(user.id)
        return 'anonymous'

    # ── capture ───────────────────────────────────────────────────────────────

    def _capture(self, request, status_code, duration_ms, exception, payload, response):
        is_error = exception is not None or status_code >= 400
        is_slow = duration_ms >= self.slow_ms
        # Always keep errors and slow requests; sample the rest to control cost.
        if not (is_error or is_slow) and self.sample_rate < 1.0:
            if random.random() > self.sample_rate:
                return

        client = get_posthog()
        if client is None:
            return

        properties = {
            'source': 'server',
            'method': request.method,
            'route': self._route(request),
            'path': request.path,
            'status_code': status_code,
            'duration_ms': round(duration_ms, 1),
            'success': not is_error,
            'slow': is_slow,
            '$process_person_profile': False,
        }
        if exception is not None:
            properties['error_type'] = type(exception).__name__
            properties['error'] = str(exception)[:_MAX_STR]

        # On failures, attach the full diagnostic context: what was sent + what
        # the API said was wrong. (Skipped on success to control event size/cost.)
        if is_error:
            query = _scrub(dict(request.GET)) if request.GET else None
            if query:
                properties['query_params'] = query
            payload_props = self._payload_props(payload)
            if payload_props is not None:
                properties['request_payload'] = payload_props
            error_detail = self._response_body(response)
            if error_detail is not None:
                properties['error_detail'] = error_detail

        client.capture(
            distinct_id=self._distinct_id(request),
            event='api_request',
            properties=properties,
        )
