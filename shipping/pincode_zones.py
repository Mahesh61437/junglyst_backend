"""
Pincode zone classifier for Junglyst shipping.

Zones:
  A/B/C — fully deliverable, normal rules apply
  D_whitelisted — 10 approved cities, normal rules apply
  E — blocked, no delivery

Rule: if pincode is in a whitelisted Zone D city → D_whitelisted (deliverable)
      if pincode is otherwise unserviceable → E (blocked)
      else → A/B/C (deliverable, labelled 'C' generically)
"""

# Pincode prefix → (zone, city_name)
# Zone D whitelisted cities (spec-mandated)
_ZONE_D_WHITELISTED = {
    "302": ("D_whitelisted", "Jaipur"),
    "303": ("D_whitelisted", "Jaipur"),
    "380": ("D_whitelisted", "Ahmedabad"),
    "382": ("D_whitelisted", "Ahmedabad"),
    "395": ("D_whitelisted", "Surat"),
    "394": ("D_whitelisted", "Surat"),
    "160": ("D_whitelisted", "Chandigarh"),
    "226": ("D_whitelisted", "Lucknow"),
    "751": ("D_whitelisted", "Bhubaneswar"),
    "403": ("D_whitelisted", "Goa"),
    "440": ("D_whitelisted", "Nagpur"),
    "441": ("D_whitelisted", "Nagpur"),
    "641": ("D_whitelisted", "Coimbatore"),
    "642": ("D_whitelisted", "Coimbatore"),
    "682": ("D_whitelisted", "Kochi"),
    "683": ("D_whitelisted", "Kochi"),
    "684": ("D_whitelisted", "Kochi"),
}

# Zone A — major metros (normal rules)
_ZONE_A_PREFIXES = {
    # Mumbai
    "400", "401", "402",
    # Delhi / NCR
    "110", "111", "112", "120", "121", "122", "123", "124", "125",
    # Bangalore
    "560", "561", "562", "563", "564", "565",
    # Chennai
    "600", "601", "602", "603", "604",
    # Hyderabad
    "500", "501", "502", "503",
    # Kolkata
    "700", "701", "702", "703", "711", "712",
    # Pune
    "410", "411", "412", "413", "414",
}

# Pincode prefixes that are blocked / unserviceable (Zone E)
# Andaman & Nicobar: 744xxx, Lakshadweep: 682551+ (small island), J&K remote: 190-194
# These cover the clear no-delivery zones; all others default to deliverable.
_BLOCKED_PREFIXES = {
    "744",  # Andaman & Nicobar
    "193",  # J&K remote (Kargil / Leh area)
    "194",  # J&K remote (Leh)
}

# Non-Indian / clearly invalid first digit
_INVALID_FIRST_DIGITS = {"0", "9"}


def classify_pincode(pincode: str) -> dict:
    """
    Returns:
    {
        "pincode": "302001",
        "deliverable": True/False,
        "zone": "A" | "C" | "D_whitelisted" | "E",
        "city": "Jaipur" | None,
        "message": "..."
    }
    """
    if not pincode or not pincode.isdigit() or len(pincode) != 6:
        return {
            "pincode": pincode,
            "deliverable": False,
            "zone": "E",
            "city": None,
            "message": "Invalid pincode format.",
        }

    prefix3 = pincode[:3]
    first_digit = pincode[0]

    # Invalid first digit
    if first_digit in _INVALID_FIRST_DIGITS:
        return {
            "pincode": pincode,
            "deliverable": False,
            "zone": "E",
            "city": None,
            "message": "Sorry, we don't deliver to your pincode yet.",
        }

    # Blocked prefixes
    if prefix3 in _BLOCKED_PREFIXES:
        return {
            "pincode": pincode,
            "deliverable": False,
            "zone": "E",
            "city": None,
            "message": "Sorry, we don't deliver to your pincode yet.",
        }

    # Zone D whitelisted cities
    if prefix3 in _ZONE_D_WHITELISTED:
        zone, city = _ZONE_D_WHITELISTED[prefix3]
        return {
            "pincode": pincode,
            "deliverable": True,
            "zone": zone,
            "city": city,
            "message": f"Delivery available to {city}.",
        }

    # Zone A metros
    if prefix3 in _ZONE_A_PREFIXES:
        return {
            "pincode": pincode,
            "deliverable": True,
            "zone": "A",
            "city": None,
            "message": "Delivery available.",
        }

    # Everything else — Zone C (deliverable, standard)
    return {
        "pincode": pincode,
        "deliverable": True,
        "zone": "C",
        "city": None,
        "message": "Delivery available.",
    }


# Zone → (min_transit_days, max_transit_days) after dispatch.
# Must stay in sync with junglyst_frontend/src/context/CartContext.jsx::transitDays.
_TRANSIT_DAYS = {
    "A": (1, 2),
    "B": (2, 3),
    "C": (3, 5),
    "D_whitelisted": (4, 6),
}


def transit_days_for_zone(zone: str | None) -> tuple[int, int]:
    """Return (min_days, max_days) of carrier transit after dispatch."""
    return _TRANSIT_DAYS.get(zone or "", (3, 5))

