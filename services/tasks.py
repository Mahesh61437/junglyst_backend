from celery import shared_task
from django.conf import settings
from django.utils import timezone
from django.utils.html import escape, strip_tags

from core.email import paced_send_mail
from shipping.regions import STATE_NAMES


def _send(subject, html, recipients):
    recipients = [recipient for recipient in recipients if recipient]
    if not recipients:
        return
    paced_send_mail(subject=subject, message=strip_tags(html), html_message=html, recipient_list=recipients)


def _days_left_text(pay_by, now=None):
    """'3 days' / '20 hours' until the pay-by date, for the quote and reminder emails."""
    hours = ((pay_by - (now or timezone.now())).total_seconds()) / 3600
    if hours < 1:
        return 'less than an hour'
    if hours < 24:
        return f'{int(hours)} hour{"s" if int(hours) != 1 else ""}'
    days = round(hours / 24)
    return f'{days} day{"s" if days != 1 else ""}'


def _pay_by_html(quote):
    if not quote.pay_by:
        return ''
    deadline = timezone.localtime(quote.pay_by)
    return (f'<p style="background:#fff7ed;border:1px solid #fdba74;border-radius:8px;padding:10px 12px;">'
            f'<strong>Please pay within {_days_left_text(quote.pay_by)}</strong><br>'
            f'by {deadline:%A, %d %B, %I:%M %p}. After that the quote expires.</p>')


def _answers_html(quote):
    rows = []
    for field in quote.form_snapshot:
        if field['key'] not in quote.answers:
            continue
        value = quote.answers[field['key']]
        value = ', '.join(value) if isinstance(value, list) else value
        rows.append(f'<tr><td style="padding:4px 12px 4px 0;color:#555;">{escape(field["label"])}</td>'
                    f'<td><strong>{escape(str(value))}</strong></td></tr>')
    return f'<table>{"".join(rows)}</table>' if rows else '<p><em>No extra details given.</em></p>'


def _request_html(quote):
    images = ''.join(f'<li><a href="{escape(url)}">{escape(url)}</a></li>' for url in quote.reference_images)
    return f"""
        <p><strong>Reference:</strong> {escape(quote.reference)}</p>
        <p><strong>Service:</strong> {escape(quote.offering.group.name)} / {escape(quote.offering.name)}</p>
        <p><strong>Expected budget:</strong> ₹{quote.budget_min:,.0f} – ₹{quote.budget_max:,.0f}</p>
        <p><strong>Customer:</strong> {escape(quote.contact_name)} · {escape(quote.contact_phone)}
           · {escape(quote.contact_email)}</p>
        <p><strong>State:</strong> {escape(STATE_NAMES.get(quote.state_code, quote.state_code))}</p>
        <h3>Requirements</h3>
        {_answers_html(quote)}
        {f'<p><strong>Notes:</strong> {escape(quote.customer_notes)}</p>' if quote.customer_notes else ''}
        {f'<p><strong>Reference images:</strong></p><ul>{images}</ul>' if images else ''}
    """


@shared_task(name='services.tasks.send_new_request_emails_task', max_retries=2, default_retry_delay=60)
def send_new_request_emails_task(quote_id):
    from core.models import User
    from .models import QuoteRequest

    quote = QuoteRequest.objects.select_related('offering__group').get(id=quote_id)
    frontend = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
    admin_emails = list(User.objects.filter(is_superuser=True, is_active=True).values_list('email', flat=True))
    _send(
        f'New quote request {quote.reference}: {quote.offering.name} (₹{quote.budget_min:,.0f}–₹{quote.budget_max:,.0f})',
        f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
            <h2>New quote request</h2>
            {_request_html(quote)}
            <p><a href="{frontend}/super-admin/services">Open the quote queue</a></p>
        </body></html>""",
        admin_emails,
    )
    _send(
        f'We received your request {quote.reference}',
        f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
            <h2>Thanks, {escape(quote.contact_name)}!</h2>
            <p>We have your request and will contact you soon with a quote.
               You can also call us if you would like to discuss it.</p>
            {_request_html(quote)}
        </body></html>""",
        [quote.contact_email],
    )


@shared_task(name='services.tasks.send_pay_request_email_task', max_retries=2, default_retry_delay=60)
def send_pay_request_email_task(quote_id):
    from .models import QuoteRequest

    quote = QuoteRequest.objects.select_related('offering').get(id=quote_id)
    frontend = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
    inclusions = ''.join(
        f'<li>{escape(item["label"])}' + (f' (₹{escape(item["amount"])})' if item['amount'] else '') + '</li>'
        for item in quote.inclusions
    )
    _send(
        f'Your quote {quote.reference} is ready to pay',
        f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
            <h2>Your quote is ready</h2>
            <p>Hi {escape(quote.contact_name)}, here is your quote for {escape(quote.offering.name)}.</p>
            <p><strong>Item price:</strong> ₹{quote.item_price} &nbsp; <strong>Shipping:</strong> ₹{quote.shipping_fee}
               &nbsp; <strong>Total:</strong> ₹{quote.total_amount}</p>
            {f'<p><strong>Size:</strong> {escape(quote.shape)}</p>' if quote.shape else ''}
            {f'<h3>Included</h3><ul>{inclusions}</ul>' if inclusions else ''}
            {f'<p>{escape(quote.quote_notes)}</p>' if quote.quote_notes else ''}
            {_pay_by_html(quote)}
            <p>Sign in and pay from <a href="{frontend}/orders">My Orders</a> to confirm. You will enter your delivery address at payment.</p>
        </body></html>""",
        [quote.contact_email],
    )


@shared_task(name='services.tasks.send_additional_charge_email_task', max_retries=2, default_retry_delay=60)
def send_additional_charge_email_task(charge_id):
    from .models import AdditionalCharge

    charge = AdditionalCharge.objects.select_related('quote_request').get(id=charge_id)
    quote = charge.quote_request
    frontend = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
    _send(
        f'Additional payment requested for {quote.reference}',
        f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
            <h2>An additional payment is needed</h2>
            <p>Hi {escape(quote.contact_name)}, there is an addition to your custom order {escape(quote.reference)}.</p>
            <p><strong>{escape(charge.description)}</strong> &nbsp; <strong>₹{charge.amount}</strong></p>
            <p>Sign in and pay from <a href="{frontend}/orders">My Orders</a>. Your delivery address stays the same.</p>
        </body></html>""",
        [quote.contact_email],
    )


@shared_task(name='services.tasks.expire_pay_requests_task')
def expire_pay_requests_task():
    from .workflow import expire_overdue_pay_requests

    return expire_overdue_pay_requests()


@shared_task(name='services.tasks.send_pay_by_reminder_email_task', max_retries=2, default_retry_delay=60)
def send_pay_by_reminder_email_task(quote_id):
    from .models import QuoteRequest

    quote = QuoteRequest.objects.select_related('offering').get(id=quote_id)
    frontend = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
    _send(
        f'Reminder: your quote {quote.reference} expires in {_days_left_text(quote.pay_by)}',
        f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
            <h2>Your quote expires soon</h2>
            <p>Hi {escape(quote.contact_name)}, your quote for {escape(quote.offering.name)} is still waiting for you.</p>
            <p><strong>Total:</strong> ₹{quote.total_amount}</p>
            {_pay_by_html(quote)}
            <p><a href="{frontend}/orders">Pay from My Orders</a> to keep it. If you need more time or want changes,
               just call us and we can sort it out.</p>
        </body></html>""",
        [quote.contact_email],
    )


@shared_task(name='services.tasks.send_cancel_request_email_task', max_retries=2, default_retry_delay=60)
def send_cancel_request_email_task(quote_id):
    from core.models import User
    from .models import QuoteRequest

    quote = QuoteRequest.objects.select_related('offering').get(id=quote_id)
    frontend = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
    admins = list(User.objects.filter(is_superuser=True, is_active=True).values_list('email', flat=True))
    _send(
        f'Cancellation requested: {quote.reference}',
        f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
            <h2>A customer asked to cancel a paid order</h2>
            <p><strong>{escape(quote.reference)}</strong> · {escape(quote.offering.name)}</p>
            <p>{escape(quote.contact_name)} · {escape(quote.contact_phone)} · {escape(quote.contact_email)}</p>
            <p><strong>Reason:</strong> {escape(quote.cancel_request_reason) or 'Not given'}</p>
            <p>Open it in the <a href="{frontend}/super-admin/services">custom orders queue</a> to cancel it (refund
               manually) or keep the order.</p>
        </body></html>""",
        admins,
    )


@shared_task(name='services.tasks.pay_by_reminders_task')
def pay_by_reminders_task():
    from .workflow import send_pay_by_reminders

    return send_pay_by_reminders()
