"""
Management command: beautify_descriptions
Converts plain-text care guide descriptions for Aquatic Exotica products to HTML.
Run: python manage.py beautify_descriptions [--dry-run] [--seller-email EMAIL]
"""
import re
from django.core.management.base import BaseCommand
from django.db import transaction

# Known section headers in plant/aquatic care guides (order matters — longer first)
SECTION_HEADERS = [
    "CO2 and Fertilization",
    "CO2 Requirements",
    "Planting and Substrate",
    "Planting Instructions",
    "Growth and Appearance",
    "Water Parameters",
    "Water Quality",
    "General Tips",
    "Aquascaping Tips",
    "Special Considerations",
    "Compatible Species",
    "Tank Setup",
    "Hardscape Tips",
    "Shipping Note",
    "Important Note",
    "Care Tips",
    "Maintenance",
    "Harvesting",
    "Propagation",
    "Fertilization",
    "Placement",
    "Lighting",
    "Feeding",
    "Storage",
    "Origin",
    "Notes",
]

# Regex that matches any section header (word-boundary, case-sensitive)
_HEADER_PATTERN = re.compile(
    r'(?<![a-z])(' + '|'.join(re.escape(h) for h in SECTION_HEADERS) + r')(?=\s)',
)


def _strip_duplicate_title(text: str) -> str:
    """Remove the doubled title header that appears at the start of descriptions.
    Pattern: '<Long Title> Care Guide <Short Title> Care Guide <intro text...>'
    We want to keep only the intro text (everything after the 2nd 'Care Guide').
    """
    # Find all positions of 'Care Guide' in the first 300 chars
    matches = [m.start() for m in re.finditer(r'Care Guide', text[:400])]
    if len(matches) >= 2:
        # Skip past the 2nd occurrence + ' '
        cut = matches[1] + len('Care Guide')
        text = text[cut:].lstrip()
    elif len(matches) == 1:
        cut = matches[0] + len('Care Guide')
        text = text[cut:].lstrip()
    return text


def _split_into_sections(text: str):
    """Split plain text into [(section_header_or_None, body_text)] list."""
    parts = _HEADER_PATTERN.split(text)
    # split() with a capturing group gives: [before, h1, after_h1, h2, after_h2, ...]
    sections = []
    if parts[0].strip():
        sections.append((None, parts[0].strip()))
    i = 1
    while i < len(parts) - 1:
        header = parts[i]
        body = parts[i + 1].strip() if i + 1 < len(parts) else ''
        sections.append((header, body))
        i += 2
    return sections


def _format_body_as_html(body: str) -> str:
    """Convert a section body into HTML list items or paragraphs.

    Entries that follow the pattern 'Label: text' become <li> items.
    Free-flowing sentences that don't match stay as <p>.
    """
    # Split on patterns like 'Word Word Word: ' — label is 1–5 words ending in colon
    # We want to split at sentence-start labels, not mid-sentence colons (e.g. '22°C:')
    bullet_pattern = re.compile(
        r'(?<!\d)(?<!\w{10})'           # not immediately after a number or long word
        r'(?:^|(?<=\.\s)|(?<=\!\s)|(?<=\?\s)|(?<=\s))'  # start or after sentence-end
        r'([A-Z][A-Za-z &\'"\/\-]{1,40})'               # 1-5 word label starting capital
        r':\s+'                                           # colon + space
    )

    items = re.split(r'([A-Z][A-Za-z ,&\'"\/\-]{1,50}):\s+', body)

    # If we got a clean split (multiple labeled items), format as <ul>
    if len(items) >= 5:  # at least 2 label+content pairs
        html_parts = []
        # items[0] might be empty or intro text
        intro = items[0].strip()
        if intro:
            html_parts.append(f'<p>{intro}</p>')
        i = 1
        while i < len(items) - 1:
            label = items[i].strip()
            content = items[i + 1].strip().rstrip('.')
            # Skip if label looks like it's mid-sentence (all lowercase after first char)
            if label and content:
                html_parts.append(f'<li><strong>{label}:</strong> {content}.</li>')
            i += 2
        if any(p.startswith('<li>') for p in html_parts):
            list_items = [p for p in html_parts if p.startswith('<li>')]
            non_list = [p for p in html_parts if not p.startswith('<li>')]
            return ''.join(non_list) + '<ul>' + ''.join(list_items) + '</ul>'
        return ''.join(html_parts)

    # Fallback: just wrap in <p>
    return f'<p>{body}</p>'


def plain_text_to_html(name: str, description: str) -> str:
    """Convert a plain-text care guide description to clean HTML."""
    if not description:
        return description

    text = description.strip()
    text = _strip_duplicate_title(text)

    sections = _split_into_sections(text)
    html_blocks = []

    for header, body in sections:
        if not body.strip():
            continue
        if header:
            html_blocks.append(f'<h3>{header}</h3>')
        html_blocks.append(_format_body_as_html(body))

    return '\n'.join(html_blocks)


class Command(BaseCommand):
    help = 'Convert plain-text product descriptions to HTML for Aquatic Exotica'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print output without saving to DB')
        parser.add_argument('--seller-email', default='accessmaheshforu@gmail.com',
                            help='Seller email to target (default: Aquatic Exotica)')
        parser.add_argument('--product-id', help='Only process one product by ID')

    def handle(self, *args, **options):
        from core.models import Product, User

        email = options['seller_email']
        try:
            seller = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'Seller not found: {email}'))
            return

        qs = Product.objects.filter(seller=seller)
        if options['product_id']:
            qs = qs.filter(id=options['product_id'])

        self.stdout.write(f'Processing {qs.count()} products for {email}...\n')

        updated = 0
        skipped = 0

        for product in qs.order_by('name'):
            if not product.description:
                self.stdout.write(f'  [SKIP - no desc] {product.name}')
                skipped += 1
                continue

            # Skip if already HTML (has tags)
            if '<h' in product.description or '<p>' in product.description or '<ul>' in product.description:
                self.stdout.write(f'  [SKIP - already HTML] {product.name}')
                skipped += 1
                continue

            new_html = plain_text_to_html(product.name, product.description)

            if options['dry_run']:
                self.stdout.write(f'\n{"="*60}')
                self.stdout.write(f'PRODUCT: {product.name}')
                self.stdout.write(f'{"="*60}')
                self.stdout.write(new_html[:800])
                self.stdout.write('...' if len(new_html) > 800 else '')
            else:
                product.description = new_html
                product.save(update_fields=['description'])
                self.stdout.write(f'  [OK] {product.name}')
                updated += 1

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('\n[DRY RUN] No changes saved.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone. {updated} products updated, {skipped} skipped.'
            ))
