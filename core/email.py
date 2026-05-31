"""Rate-paced email sending against Resend's 2-req/sec free tier.

Per-process pacing — sufficient today because all transactional email is
sent from a single Celery worker. When we scale to multiple workers we
will need a shared limiter (Redis token bucket) or Resend's batch API.
"""
import logging
import time

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

# Resend free tier allows 2 requests/sec. 600ms gives headroom for clock
# skew and request latency variance.
_MIN_INTERVAL_S = 0.6
_last_send_ts = 0.0


def paced_send_mail(
    *,
    subject,
    message,
    recipient_list,
    html_message=None,
    from_email=None,
    max_retries=3,
):
    """send_mail with per-process pacing + 429 retry."""
    global _last_send_ts
    from anymail.exceptions import AnymailRequestsAPIError

    attempt = 0
    while True:
        gap = _MIN_INTERVAL_S - (time.monotonic() - _last_send_ts)
        if gap > 0:
            time.sleep(gap)

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email or settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipient_list,
                html_message=html_message,
                fail_silently=False,
            )
            _last_send_ts = time.monotonic()
            return
        except AnymailRequestsAPIError as exc:
            _last_send_ts = time.monotonic()
            status_code = getattr(exc, 'status_code', None)
            if status_code != 429 or attempt >= max_retries:
                raise

            backoff = _MIN_INTERVAL_S * (2 ** attempt)
            resp = getattr(exc, 'response', None)
            if resp is not None:
                header = resp.headers.get('Retry-After')
                if header:
                    try:
                        backoff = max(backoff, float(header))
                    except ValueError:
                        pass

            logger.warning(
                "Resend 429 for %s, retrying in %.2fs (attempt %d/%d)",
                recipient_list, backoff, attempt + 1, max_retries,
            )
            time.sleep(backoff)
            attempt += 1
