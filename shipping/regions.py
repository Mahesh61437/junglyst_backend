"""
Delivery regions for region-based shopping.

India is split into three delivery regions:
  south       — Andhra Pradesh, Telangana, Tamil Nadu, Karnataka, Kerala
                (plus the Puducherry and Lakshadweep enclaves of those states)
  north_east  — the seven sister states and Sikkim
  north       — every other state / UT

Buyers pick a state (or share their location); the state resolves to a
region. Sellers choose which regions they ship to and may set a flat
delivery fee per region.
"""

REGION_SOUTH = 'south'
REGION_NORTH = 'north'
REGION_NORTH_EAST = 'north_east'

REGION_LABELS = {
    REGION_SOUTH: 'South India',
    REGION_NORTH: 'North India',
    REGION_NORTH_EAST: 'North East India',
}

REGION_CODES = tuple(REGION_LABELS)

# (code, name, region, aliases). Aliases are matched case-insensitively with
# punctuation and spaces stripped, so "A.P", "a p" and "AP" all resolve.
_STATES = (
    ('AP', 'Andhra Pradesh', REGION_SOUTH, ()),
    ('TS', 'Telangana', REGION_SOUTH, ('TG',)),
    ('TN', 'Tamil Nadu', REGION_SOUTH, ()),
    ('KA', 'Karnataka', REGION_SOUTH, ()),
    ('KL', 'Kerala', REGION_SOUTH, ()),
    ('PY', 'Puducherry', REGION_SOUTH, ('Pondicherry',)),
    ('LD', 'Lakshadweep', REGION_SOUTH, ()),

    ('AS', 'Assam', REGION_NORTH_EAST, ()),
    ('AR', 'Arunachal Pradesh', REGION_NORTH_EAST, ()),
    ('MN', 'Manipur', REGION_NORTH_EAST, ()),
    ('ML', 'Meghalaya', REGION_NORTH_EAST, ()),
    ('MZ', 'Mizoram', REGION_NORTH_EAST, ()),
    ('NL', 'Nagaland', REGION_NORTH_EAST, ()),
    ('TR', 'Tripura', REGION_NORTH_EAST, ()),
    ('SK', 'Sikkim', REGION_NORTH_EAST, ()),

    ('DL', 'Delhi', REGION_NORTH, ('New Delhi', 'NCT of Delhi', 'National Capital Territory of Delhi')),
    ('HR', 'Haryana', REGION_NORTH, ()),
    ('PB', 'Punjab', REGION_NORTH, ()),
    ('CH', 'Chandigarh', REGION_NORTH, ()),
    ('HP', 'Himachal Pradesh', REGION_NORTH, ()),
    ('JK', 'Jammu and Kashmir', REGION_NORTH, ('Jammu & Kashmir', 'J&K')),
    ('LA', 'Ladakh', REGION_NORTH, ()),
    ('UK', 'Uttarakhand', REGION_NORTH, ('Uttaranchal',)),
    ('UP', 'Uttar Pradesh', REGION_NORTH, ()),
    ('RJ', 'Rajasthan', REGION_NORTH, ()),
    ('GJ', 'Gujarat', REGION_NORTH, ()),
    ('MH', 'Maharashtra', REGION_NORTH, ()),
    ('GA', 'Goa', REGION_NORTH, ()),
    ('MP', 'Madhya Pradesh', REGION_NORTH, ()),
    ('CG', 'Chhattisgarh', REGION_NORTH, ('CT', 'Chattisgarh')),
    ('BR', 'Bihar', REGION_NORTH, ()),
    ('JH', 'Jharkhand', REGION_NORTH, ()),
    ('WB', 'West Bengal', REGION_NORTH, ()),
    ('OD', 'Odisha', REGION_NORTH, ('OR', 'Orissa')),
    ('DN', 'Dadra and Nagar Haveli and Daman and Diu', REGION_NORTH,
     ('DH', 'Dadra and Nagar Haveli', 'Daman and Diu', 'Daman & Diu')),
    ('AN', 'Andaman and Nicobar Islands', REGION_NORTH, ('Andaman & Nicobar Islands', 'Andaman and Nicobar')),
)


def _normalize(value: str) -> str:
    return ''.join(ch for ch in value.lower() if ch.isalnum())


STATE_NAMES = {code: name for code, name, _region, _aliases in _STATES}
STATE_REGIONS = {code: region for code, _name, region, _aliases in _STATES}
STATE_CODES = tuple(STATE_NAMES)

_STATE_CODE_BY_KEY = {}
for _code, _name, _region, _aliases in _STATES:
    for _key in (_code, _name, *_aliases):
        _STATE_CODE_BY_KEY[_normalize(_key)] = _code


def states_payload() -> list:
    """Public list of states for the region picker."""
    return [
        {'code': code, 'name': name, 'region': region, 'aliases': list(aliases)}
        for code, name, region, aliases in _STATES
    ]


def regions_payload() -> list:
    return [{'code': code, 'label': label} for code, label in REGION_LABELS.items()]


def is_valid_region(region) -> bool:
    return region in REGION_LABELS


def state_code_for(state) -> str | None:
    """State code (e.g. 'KL') for a state name, code or common abbreviation; None if unknown."""
    if not state or not isinstance(state, str):
        return None
    return _STATE_CODE_BY_KEY.get(_normalize(state))


def region_for_state(state) -> str | None:
    """Region for a state name, code or common abbreviation; None if unknown."""
    return STATE_REGIONS.get(state_code_for(state))


# First two pincode digits identify the postal circle (state).
_SOUTH_PIN_PREFIXES = {50, 51, 52, 53} | set(range(56, 70))
_NORTH_EAST_PIN_PREFIXES = {78, 79}
_SIKKIM_PIN_PREFIX = '737'


def region_for_pincode(pincode) -> str | None:
    """Region for a 6-digit Indian pincode; None if the pincode is malformed."""
    pincode = str(pincode or '').strip()
    if len(pincode) != 6 or not pincode.isdigit() or pincode[0] == '0':
        return None
    if pincode.startswith(_SIKKIM_PIN_PREFIX):
        return REGION_NORTH_EAST
    prefix = int(pincode[:2])
    if prefix in _SOUTH_PIN_PREFIXES:
        return REGION_SOUTH
    if prefix in _NORTH_EAST_PIN_PREFIXES:
        return REGION_NORTH_EAST
    return REGION_NORTH


# Pincode postal circle (first two digits) → states it can belong to. Circles
# don't follow state borders exactly, so each lists every state it spans
# (e.g. 16 covers Punjab and Chandigarh, 53 includes Yanam in Puducherry).
# Unlisted prefixes (e.g. 9x Army Post Office) are not checked.
_PINCODE_CIRCLE_STATES = {
    '11': ('DL',),
    '12': ('HR',), '13': ('HR', 'PB'),
    '14': ('PB',), '15': ('PB',), '16': ('PB', 'CH', 'HR'),
    '17': ('HP',),
    '18': ('JK',), '19': ('JK', 'LA'),
    **{str(p): ('UP', 'UK') for p in range(20, 29)},
    **{str(p): ('RJ',) for p in range(30, 35)},
    **{str(p): ('GJ', 'DN') for p in range(36, 40)},
    **{str(p): ('MH', 'GA', 'DN') for p in range(40, 45)},
    **{str(p): ('MP', 'CG') for p in range(45, 49)},
    '49': ('CG',),
    '50': ('TS', 'AP'), '51': ('AP', 'TS'), '52': ('AP', 'TS'), '53': ('AP', 'TS', 'PY'),
    **{str(p): ('KA',) for p in range(56, 60)},
    **{str(p): ('TN', 'PY') for p in range(60, 65)},
    **{str(p): ('KL', 'LD', 'PY') for p in range(67, 70)},
    **{str(p): ('WB', 'SK', 'AN') for p in range(70, 75)},
    **{str(p): ('OD',) for p in range(75, 78)},
    '78': ('AS', 'ML', 'AR', 'NL'),
    '79': ('AR', 'ML', 'MN', 'MZ', 'NL', 'TR', 'AS'),
    **{str(p): ('BR', 'JH') for p in range(80, 86)},
}


def states_for_pincode(pincode) -> tuple:
    """State codes a 6-digit pincode can belong to; empty when malformed or unmapped."""
    pincode = str(pincode or '').strip()
    if len(pincode) != 6 or not pincode.isdigit():
        return ()
    return _PINCODE_CIRCLE_STATES.get(pincode[:2], ())


def pincode_matches_state(pincode, state_code) -> bool:
    """False only when both are known and the pincode can't be in that state."""
    candidates = states_for_pincode(pincode)
    return not (candidates and state_code) or state_code in candidates


def resolve_address_region(state=None, pincode=None) -> str | None:
    """
    Region of a delivery address. The pincode is authoritative (it is what the
    courier routes on); the free-text state is the fallback.
    """
    return region_for_pincode(pincode) or region_for_state(state)
