"""
Management command: force-sync the status of one or more shipments from the
live Shiprocket / NimbusPost API, then roll the result up to SubOrder + Order.

Usage
-----
# Re-sync a single AWB (e.g. a manually-cancelled Shiprocket order):
python manage.py sync_shipment_status SRSP8260428047

# Force a specific status without calling the courier API:
python manage.py sync_shipment_status SRSP8260428047 --force-status cancelled

# Re-sync multiple AWBs at once:
python manage.py sync_shipment_status AWB1 AWB2 AWB3
"""

from django.core.management.base import BaseCommand, CommandError
from shipping.models import Shipment
from shipping.services import get_logistics_service
from shipping.views import _sync_order_status
from orders.models import SubOrder, Order


# Courier API status -> SubOrder status (covers both NimbusPost and Shiprocket)
TRACKING_STATUS_MAP = {
    # NimbusPost-style (underscore)
    "booked":            "booked",
    "pickup_scheduled":  "booked",
    "picked_up":         "shipped",
    "in_transit":        "in_transit",
    "out_for_delivery":  "out_for_delivery",
    "delivered":         "delivered",
    "delivery_failed":   "delivery_failed",
    "rto_initiated":     "delivery_failed",
    "rto_delivered":     "cancelled",
    "cancelled":         "cancelled",
    # Shiprocket-style (space-separated, lower-cased)
    "pickup scheduled":           "booked",
    "out for pickup":             "booked",
    "picked up":                  "shipped",
    "shipped":                    "shipped",
    "in transit":                 "in_transit",
    "reached at destination hub": "in_transit",
    "delayed":                    "in_transit",
    "out for delivery":           "out_for_delivery",
    "undelivered":                "delivery_failed",
    "delivery failed":            "delivery_failed",
    "rto initiated":              "delivery_failed",
    "rto delivered":              "cancelled",
    "rto acknowledged":           "cancelled",
    "shipment cancelled":         "cancelled",
    "order cancelled":            "cancelled",
    "cancellation requested":     "cancelled",
    "canceled":                   "cancelled",
    "lost":                       "delivery_failed",
    "damaged":                    "delivery_failed",
}

VALID_FORCE_STATUSES = {
    "booked", "shipped", "in_transit", "out_for_delivery",
    "delivered", "delivery_failed", "cancelled",
}


def _apply_status(awb: str, new_sub_status: str, stdout, style):
    """Write new_sub_status to Shipment + SubOrder + Order for the given AWB."""
    updated_shipments = Shipment.objects.filter(awb_number=awb)
    if not updated_shipments.exists():
        stdout.write(style.ERROR(f"  No Shipment row found for AWB {awb}"))
        return

    updated_shipments.update(status=new_sub_status)
    sub_orders = SubOrder.objects.filter(awb_number=awb)
    sub_orders.update(status=new_sub_status)

    order_ids = list(updated_shipments.values_list("order_id", flat=True))
    for oid in order_ids:
        _sync_order_status(oid)

    stdout.write(style.SUCCESS(
        f"  AWB {awb} -> SubOrder/Shipment: {new_sub_status} | Order rolled up"
    ))


class Command(BaseCommand):
    help = "Force-sync shipment status from the courier API (or apply a status directly)."

    def add_arguments(self, parser):
        parser.add_argument(
            "awb_numbers",
            nargs="+",
            type=str,
            help="One or more AWB numbers to sync.",
        )
        parser.add_argument(
            "--force-status",
            dest="force_status",
            default=None,
            help=(
                f"Skip the courier API and apply this status directly. "
                f"Choices: {', '.join(sorted(VALID_FORCE_STATUSES))}"
            ),
        )

    def handle(self, *args, **options):
        awbs = [a.strip() for a in options["awb_numbers"] if a.strip()]
        force_status = options.get("force_status")

        if force_status is not None:
            force_status = force_status.strip().lower()
            if force_status not in VALID_FORCE_STATUSES:
                raise CommandError(
                    f"Invalid --force-status '{force_status}'. "
                    f"Choose from: {', '.join(sorted(VALID_FORCE_STATUSES))}"
                )

        service = get_logistics_service()

        for awb in awbs:
            self.stdout.write(f"\nProcessing AWB: {awb}")

            if force_status:
                self.stdout.write(f"  Forcing status -> {force_status}")
                _apply_status(awb, force_status, self.stdout, self.style)
                continue

            # Live API call
            result = service.track_shipment(awb)
            if not result or not result.get("status"):
                self.stdout.write(
                    self.style.WARNING(f"  Could not fetch tracking info from courier API.")
                )
                continue

            tracking = result.get("data", {})
            raw = (
                tracking.get("shipment_status")
                or tracking.get("current_status")
                or tracking.get("status")
                or ""
            ).lower().strip()

            self.stdout.write(f"  Courier API returned status: '{raw}'")

            new_status = TRACKING_STATUS_MAP.get(raw)
            if not new_status:
                # Also try underscore normalisation (covers NimbusPost)
                new_status = TRACKING_STATUS_MAP.get(raw.replace(" ", "_"))

            if not new_status:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Status '{raw}' not in mapping — use --force-status to override manually."
                    )
                )
                continue

            _apply_status(awb, new_status, self.stdout, self.style)

        self.stdout.write("\nDone.")
