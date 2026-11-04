"""Input rules shared by every service form (quote request, quote edit, pay address)."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from rest_framework import serializers

from sellers.models import REGION_SHIPS_FIELDS
from shipping.pincode_zones import classify_pincode
from shipping.regions import (
    STATE_NAMES, pincode_matches_state, region_for_state, state_code_for,
)

from .models import FormFieldType

INDIAN_MOBILE = re.compile(r'^[6-9]\d{9}$')
PERSON_NAME = re.compile(r"^[A-Za-z][A-Za-z .'\-]*$")
PINCODE = re.compile(r'^[1-9]\d{5}$')

BUDGET_CEILING = Decimal('1000000')
MONEY_CEILING = Decimal('10000000')
MAX_REFERENCE_IMAGES = 5
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp')}
MAX_INCLUSIONS = 30
MAX_DIMENSION_CM = Decimal('1000')
MAX_TEXT_ANSWER = 2000
MAX_CHOICES = 40


def normalise_phone(value):
    """10-digit Indian mobile from input like '+91 98765-43210'."""
    digits = re.sub(r'[\s\-()]', '', str(value or ''))
    digits = re.sub(r'^(\+91|91|0)(?=\d{10}$)', '', digits)
    if not INDIAN_MOBILE.match(digits):
        raise serializers.ValidationError('Enter a valid 10-digit mobile number.')
    return digits


def validate_person_name(value):
    value = ' '.join(str(value or '').split())
    if len(value) < 2 or len(value) > 120 or not PERSON_NAME.match(value):
        raise serializers.ValidationError('Enter a valid name (letters only, at least 2 characters).')
    return value


def validate_state(value):
    code = state_code_for(value)
    if code is None:
        raise serializers.ValidationError('Select a valid state.')
    return code


def validate_pincode(pincode, state_code):
    """6-digit pincode that exists, is deliverable, and belongs to `state_code`."""
    pincode = str(pincode or '').strip()
    if not PINCODE.match(pincode):
        raise serializers.ValidationError('Enter a valid 6-digit pincode.')
    if not classify_pincode(pincode)['deliverable']:
        raise serializers.ValidationError("Sorry, we don't deliver to this pincode yet.")
    if not pincode_matches_state(pincode, state_code):
        raise serializers.ValidationError(
            f'Pincode {pincode} does not belong to {STATE_NAMES.get(state_code, "the selected state")}. '
            'Please check your state or pincode.'
        )
    return pincode


def seller_ships_to(seller, state_code):
    """True when the seller's store-wide delivery settings reach `state_code`."""
    profile = getattr(seller, 'seller_profile', None)
    if profile is None:
        return False
    region_field = REGION_SHIPS_FIELDS.get(region_for_state(state_code))
    if region_field is None or not getattr(profile, region_field):
        return False
    return not profile.excluded_states.filter(state_code=state_code).exists()


def validate_budget(budget_min, budget_max):
    if budget_min <= 0:
        raise serializers.ValidationError({'budget_min': 'Budget must be greater than zero.'})
    if budget_max < budget_min:
        raise serializers.ValidationError({'budget_max': 'Maximum budget cannot be below the minimum.'})
    if budget_max > BUDGET_CEILING:
        raise serializers.ValidationError({'budget_max': f'Budget cannot exceed ₹{BUDGET_CEILING:,.0f}.'})


def validate_money(value, field_name, allow_zero=True):
    if value < 0 or (value == 0 and not allow_zero):
        message = 'Amount cannot be negative.' if allow_zero else 'Amount must be greater than zero.'
        raise serializers.ValidationError({field_name: message})
    if value > MONEY_CEILING:
        raise serializers.ValidationError({field_name: f'Amount cannot exceed ₹{MONEY_CEILING:,.0f}.'})
    return value


def validate_dimension(value, field_name):
    if value is None:
        return value
    if value <= 0 or value > MAX_DIMENSION_CM:
        raise serializers.ValidationError({field_name: f'Enter a size between 0 and {MAX_DIMENSION_CM} cm.'})
    return value


def validate_reference_images(urls):
    if not isinstance(urls, list) or len(urls) > MAX_REFERENCE_IMAGES:
        raise serializers.ValidationError(f'You can add up to {MAX_REFERENCE_IMAGES} reference images.')
    for url in urls:
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != 'https' or not parsed.netloc or len(url) > 500:
            raise serializers.ValidationError('Reference images must be valid https links.')
    return urls


def validate_inclusions(items):
    """[{label, amount?}] → cleaned list with amounts as 2-decimal strings."""
    if not isinstance(items, list) or len(items) > MAX_INCLUSIONS:
        raise serializers.ValidationError(f'At most {MAX_INCLUSIONS} inclusions are allowed.')
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            raise serializers.ValidationError('Each inclusion needs a label.')
        label = ' '.join(str(item.get('label', '')).split())
        if not 1 <= len(label) <= 120:
            raise serializers.ValidationError('Each inclusion needs a label of up to 120 characters.')
        amount = item.get('amount')
        if amount in (None, ''):
            cleaned.append({'label': label, 'amount': None})
            continue
        try:
            value = Decimal(str(amount))
        except InvalidOperation:
            raise serializers.ValidationError(f'"{label}" has an invalid amount.')
        if value != value.quantize(Decimal('0.01')) or value < 0 or value > MONEY_CEILING:
            raise serializers.ValidationError(f'"{label}" has an invalid amount.')
        cleaned.append({'label': label, 'amount': f'{value:.2f}'})
    return cleaned


def validate_form_field_definition(field_type, choices, min_value, max_value):
    """Rules for an admin-built form field, so a broken form can't be saved."""
    if field_type in (FormFieldType.SELECT, FormFieldType.MULTISELECT):
        if not isinstance(choices, list) or not 1 <= len(choices) <= MAX_CHOICES:
            raise serializers.ValidationError({'choices': 'Add at least one choice.'})
        if any(not isinstance(choice, str) or not choice.strip() for choice in choices) or len(set(choices)) != len(choices):
            raise serializers.ValidationError({'choices': 'Choices must be unique, non-empty text.'})
    if min_value is not None and max_value is not None and min_value > max_value:
        raise serializers.ValidationError({'max_value': 'Maximum cannot be below the minimum.'})


# ── Dynamic form answers ─────────────────────────────────────────────────────

def _clean_text(field, value):
    if not isinstance(value, str):
        raise serializers.ValidationError('Enter text.')
    value = value.strip()
    limit = min(field['max_length'] or MAX_TEXT_ANSWER, MAX_TEXT_ANSWER)
    if len(value) > limit:
        raise serializers.ValidationError(f'Keep this under {limit} characters.')
    return value


def _clean_number(field, value):
    if isinstance(value, bool):
        raise serializers.ValidationError('Enter a number.')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise serializers.ValidationError('Enter a number.')
    if not number.is_finite():
        raise serializers.ValidationError('Enter a number.')
    if field['min_value'] is not None and number < Decimal(field['min_value']):
        raise serializers.ValidationError(f'Must be at least {field["min_value"]}.')
    if field['max_value'] is not None and number > Decimal(field['max_value']):
        raise serializers.ValidationError(f'Must be at most {field["max_value"]}.')
    return str(number.normalize()) if number == number.to_integral() else str(number)


def _clean_select(field, value):
    if value not in field['choices']:
        raise serializers.ValidationError('Choose one of the listed options.')
    return value


def _clean_multiselect(field, value):
    if not isinstance(value, list) or any(choice not in field['choices'] for choice in value):
        raise serializers.ValidationError('Choose from the listed options.')
    return list(dict.fromkeys(value))


def _clean_date(field, value):
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        raise serializers.ValidationError('Enter a valid date.')
    if parsed < date.today():
        raise serializers.ValidationError('Date cannot be in the past.')
    return parsed.isoformat()


_CLEANERS = {
    FormFieldType.TEXT: _clean_text,
    FormFieldType.TEXTAREA: _clean_text,
    FormFieldType.NUMBER: _clean_number,
    FormFieldType.SELECT: _clean_select,
    FormFieldType.MULTISELECT: _clean_multiselect,
    FormFieldType.DATE: _clean_date,
}


def snapshot_form(form_fields):
    """Plain-dict copy of an offering's questions, stored on each request."""
    return [
        {
            'key': f.key, 'label': f.label, 'field_type': f.field_type, 'required': f.required,
            'choices': f.choices, 'min_value': str(f.min_value) if f.min_value is not None else None,
            'max_value': str(f.max_value) if f.max_value is not None else None, 'max_length': f.max_length,
        }
        for f in form_fields
    ]


def validate_answers(snapshot, answers):
    """Check `answers` against the question snapshot; return the cleaned answers.

    Unknown keys are rejected, required questions must be answered, and each value
    must satisfy its question's type and limits. Errors are keyed by question key.
    """
    if not isinstance(answers, dict):
        raise serializers.ValidationError({'answers': 'Answers must be an object.'})
    known = {field['key'] for field in snapshot}
    errors = {key: 'This question does not exist.' for key in answers if key not in known}
    cleaned = {}
    for field in snapshot:
        value = answers.get(field['key'])
        if value in (None, '', []):
            if field['required']:
                errors[field['key']] = 'This is required.'
            continue
        try:
            cleaned[field['key']] = _CLEANERS[field['field_type']](field, value)
        except serializers.ValidationError as exc:
            errors[field['key']] = exc.detail[0]
    if errors:
        raise serializers.ValidationError({'answers': errors})
    return cleaned


def validate_reference_upload(file_obj):
    """A real JPEG/PNG/WEBP image under 5 MB, checked by content (not by name).

    Renames the file and fixes its content type from what the bytes actually are,
    so a renamed script can never be stored as an image.
    """
    from PIL import Image, UnidentifiedImageError

    if file_obj is None:
        raise serializers.ValidationError('Choose a photo to upload.')
    if file_obj.size > MAX_UPLOAD_BYTES:
        raise serializers.ValidationError('Photos must be under 5 MB.')
    try:
        with Image.open(file_obj) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise serializers.ValidationError('Upload a JPG, PNG or WEBP photo.')
    if image_format not in UPLOAD_FORMATS:
        raise serializers.ValidationError('Upload a JPG, PNG or WEBP photo.')
    extension, content_type = UPLOAD_FORMATS[image_format]
    file_obj.seek(0)
    file_obj.name = f'reference.{extension}'
    file_obj.content_type = content_type
    return file_obj
