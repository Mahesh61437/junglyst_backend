import json
import logging
from unittest.mock import patch, MagicMock

from django.http import JsonResponse
from django.test import SimpleTestCase, RequestFactory, override_settings

from core.posthog_client import PostHogLoggingHandler
from core.middleware import PostHogAPIMetricsMiddleware, _scrub


class PostHogLoggingHandlerTests(SimpleTestCase):
    def _emit(self, make_record, name='ut_backend_log'):
        # Isolated logger (propagate=False) so we only exercise our handler,
        # not the app's real PostHog handlers wired in settings.LOGGING.
        handler = PostHogLoggingHandler()
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        try:
            make_record(logger)
        finally:
            logger.removeHandler(handler)

    def test_error_with_traceback_is_captured(self):
        client = MagicMock()
        with patch('core.posthog_client.get_posthog', return_value=client):
            def go(logger):
                try:
                    raise ValueError('boom')
                except ValueError:
                    logger.exception('something failed')
            self._emit(go)

        self.assertEqual(client.capture.call_count, 1)
        kwargs = client.capture.call_args.kwargs
        self.assertEqual(kwargs['event'], 'backend_log')
        props = kwargs['properties']
        self.assertEqual(props['event_kind'], 'error')
        self.assertEqual(props['error_type'], 'ValueError')
        self.assertIn('boom', props['error'])
        self.assertIn('ValueError', props['traceback'])
        self.assertEqual(props['level'], 'ERROR')

    def test_warning_is_captured_as_log(self):
        client = MagicMock()
        with patch('core.posthog_client.get_posthog', return_value=client):
            self._emit(lambda logger: logger.warning('heads up %s', 'now'))
        props = client.capture.call_args.kwargs['properties']
        self.assertEqual(props['event_kind'], 'log')
        self.assertEqual(props['message'], 'heads up now')

    def test_noop_when_posthog_disabled(self):
        with patch('core.posthog_client.get_posthog', return_value=None):
            # Must not raise even with no client configured.
            self._emit(lambda logger: logger.error('no client'))

    def test_skips_posthog_sdk_loggers(self):
        client = MagicMock()
        handler = PostHogLoggingHandler()
        sdk_logger = logging.getLogger('posthog.client')
        sdk_logger.addHandler(handler)
        try:
            with patch('core.posthog_client.get_posthog', return_value=client):
                sdk_logger.error('delivery failed')
        finally:
            sdk_logger.removeHandler(handler)
        client.capture.assert_not_called()


@override_settings(POSTHOG_API_KEY='phc_test')
class ApiMetricsErrorContextTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def _run(self, view, path='/api/core/products/create/', body=None):
        client = MagicMock()
        mw = PostHogAPIMetricsMiddleware(view)
        req = self.rf.post(
            path,
            data=json.dumps(body) if body is not None else '',
            content_type='application/json',
        )
        with patch('core.middleware.get_posthog', return_value=client):
            try:
                mw(req)
            except Exception:
                pass
        return client

    def test_4xx_captures_payload_and_error_detail_with_redaction(self):
        def view(request):
            return JsonResponse({'email': ['This field is required.']}, status=400)

        client = self._run(view, body={'name': '', 'password': 'hunter2', 'email': ''})
        props = client.capture.call_args.kwargs['properties']

        self.assertEqual(props['status_code'], 400)
        self.assertFalse(props['success'])
        # What was sent — with the password redacted
        self.assertEqual(props['request_payload']['name'], '')
        self.assertEqual(props['request_payload']['password'], '[REDACTED]')
        # What went wrong — the API's own error detail
        self.assertEqual(props['error_detail']['email'], ['This field is required.'])

    def test_5xx_exception_captures_payload_and_type(self):
        def view(request):
            raise ValueError('kaboom')

        client = self._run(view, body={'name': 'x', 'token': 'abc'})
        props = client.capture.call_args.kwargs['properties']

        self.assertEqual(props['status_code'], 500)
        self.assertEqual(props['error_type'], 'ValueError')
        self.assertIn('kaboom', props['error'])
        self.assertEqual(props['request_payload']['token'], '[REDACTED]')

    def test_success_omits_payload(self):
        def view(request):
            return JsonResponse({'ok': True}, status=200)

        client = self._run(view, body={'name': 'x'})
        props = client.capture.call_args.kwargs['properties']
        self.assertTrue(props['success'])
        self.assertNotIn('request_payload', props)
        self.assertNotIn('error_detail', props)

    def test_scrub_redacts_nested_sensitive_keys(self):
        cleaned = _scrub({'card': {'cvv': '123'}, 'items': [{'authToken': 't'}], 'name': 'ok'})
        self.assertEqual(cleaned['card'], '[REDACTED]')
        self.assertEqual(cleaned['items'][0]['authToken'], '[REDACTED]')
        self.assertEqual(cleaned['name'], 'ok')
