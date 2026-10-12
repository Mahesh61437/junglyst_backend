import logging

from celery import shared_task
from django.conf import settings
from django.utils.html import escape, strip_tags

from core.email import paced_send_mail

logger = logging.getLogger(__name__)


def _booking_summary_html(booking):
    from .services import organiser_contact

    workshop = booking.workshop
    venue = ', '.join(part for part in (workshop.venue_name, workshop.city) if part)
    contact = organiser_contact(workshop)
    contact_html = (
        f'<p><strong>Organiser contact:</strong> {escape(contact["name"])}'
        + (f' · <a href="tel:{escape(contact["phone"])}">{escape(contact["phone"])}</a>' if contact['phone'] else '')
        + '</p>'
    )
    map_link = (
        f'<p><a href="{escape(workshop.map_url)}">Open venue in Maps</a></p>' if workshop.map_url else ''
    )
    return f"""
        <p><strong>Booking ref:</strong> {escape(booking.reference)}</p>
        <p><strong>Workshop:</strong> {escape(booking.workshop_title)} ({escape(booking.option_name)})</p>
        <p><strong>When:</strong> {booking.session_date:%A, %d %B %Y},
           {booking.session_start:%I:%M %p} – {booking.session_end:%I:%M %p}</p>
        <p><strong>Where:</strong> {escape(venue) or 'Venue details will be shared by the organiser'}</p>
        {map_link}
        <p><strong>Attendee:</strong> {escape(booking.attendee_name)} · {escape(booking.attendee_phone)}
           · {escape(booking.attendee_email)}</p>
        <p><strong>Seats:</strong> {booking.participants} &nbsp; <strong>Paid:</strong> ₹{booking.total_amount}</p>
        {contact_html}
    """


def _send(subject, html, recipients):
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    paced_send_mail(
        subject=subject,
        message=strip_tags(html),
        html_message=html,
        recipient_list=recipients,
    )


@shared_task(name='workshops.tasks.send_workshop_booking_emails_task', max_retries=2, default_retry_delay=60)
def send_workshop_booking_emails_task(booking_id):
    from core.models import User
    from .models import WorkshopBooking

    booking = WorkshopBooking.objects.select_related('workshop', 'order').get(id=booking_id)
    summary = _booking_summary_html(booking)
    frontend = getattr(settings, 'FRONTEND_URL', '').rstrip('/')

    try:
        _send(
            f"You're booked: {booking.workshop_title}",
            f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
                <h2>See you at the workshop!</h2>
                <p>Hi {escape(booking.attendee_name)}, your seat is confirmed.</p>
                {summary}
                <p>Please arrive 10 minutes early. Keep this email handy at the venue.</p>
                <p><a href="{frontend}/workshops/{escape(booking.workshop.slug)}">View workshop details</a></p>
                <p>Questions? Write to admin@junglyst.com</p>
            </body></html>""",
            [booking.attendee_email],
        )
        for split in booking.splits.select_related('seller'):
            _send(
                f"New workshop booking: {booking.workshop_title} ({booking.reference})",
                f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
                    <h2>New booking for your workshop</h2>
                    {summary}
                    <p>Your share ({split.share_percent}%): ₹{split.gross_amount}
                       · Your payout after {split.commission_percent}% commission: ₹{split.seller_payout}</p>
                </body></html>""",
                [split.seller.email],
            )
        admin_emails = list(User.objects.filter(is_superuser=True, is_deleted=False).values_list('email', flat=True))
        _send(
            f"[Workshop] {booking.reference} · {booking.workshop_title}",
            f"""<html><body style="font-family: Arial, sans-serif;">
                <h2>Workshop booking confirmed</h2>
                {summary}
                <p>Junglyst commission: ₹{booking.commission_amount}
                   · Organiser payouts: ₹{booking.seller_payout}</p>
            </body></html>""",
            admin_emails,
        )
    except Exception:
        logger.exception("Workshop booking emails failed for %s", booking.reference)
        raise


@shared_task(name='workshops.tasks.send_session_rescheduled_task', max_retries=2, default_retry_delay=60)
def send_session_rescheduled_task(session_id, previous_label):
    """Tell everyone holding a paid seat that their session moved."""
    from orders.bookings import PAID_BOOKING_STATUSES
    from .models import WorkshopBooking

    bookings = (WorkshopBooking.objects.filter(session_id=session_id, status__in=PAID_BOOKING_STATUSES)
                .select_related('workshop', 'order'))
    for booking in bookings:
        try:
            _send(
                f"Updated timing: {booking.workshop_title}",
                f"""<html><body style="font-family: Arial, sans-serif; color: #1b2d2a;">
                    <h2>Your workshop session has a new time</h2>
                    <p>Hi {escape(booking.attendee_name)}, the session you booked
                       (previously {escape(previous_label)}) has been rescheduled.</p>
                    {_booking_summary_html(booking)}
                    <p>If the new time doesn't work for you, reply to admin@junglyst.com with your booking reference.</p>
                </body></html>""",
                [booking.attendee_email],
            )
        except Exception:
            logger.exception("Reschedule email failed for %s", booking.reference)
