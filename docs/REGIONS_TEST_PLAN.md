# Region-based shopping — test plan

Covers buyer delivery location, seller delivery regions / excluded states / category restrictions / regional fees,
and the checkout address checks. Last run: 2026-10-11.

## How to run

Backend (always point at local SQLite + in-memory cache — the `.env` targets Railway and
`cache.clear()` would flush that Redis):

```bash
DB_HOST= REDIS_URL= REDISHOST= .venv/Scripts/python.exe manage.py test shipping sellers core.tests_regions orders.tests_regions
```

Frontend (Node's built-in runner, no extra dependencies), from `junglyst_ui`:

```bash
npm test
```

## Automated coverage

| Area | File | What it checks |
|---|---|---|
| State / region lookup | `shipping/tests.py` | Names, codes, aliases (`A.P`, `TG`, `Orissa`); unknown input; every state maps to its region |
| PIN code → region / state | `shipping/tests.py` | South / North / North East prefixes incl. Sikkim `737`; malformed PINs; PIN-state matching (`517128` ≠ Himachal); unmapped APO prefixes not flagged; every state reachable |
| Public endpoints | `shipping/tests.py` | `/shipping/regions/` (36 unique states, cache header); `pincode-check` returns `region` + `states`, rejects malformed PINs |
| Seller settings model | `sellers/tests_delivery_regions.py` | Defaults to all India; `ships_to`, `service_regions`, fees, excluded state codes |
| Seller settings API | `sellers/tests_delivery_regions.py` | GET/PATCH, fee bounds (0–5000, whole rupees), at least one region and one state, exclusions replace / clear / keep / dedupe, unknown codes rejected, cache cleared only on change, auth (anonymous 401, buyer 403, no profile 404, own profile only) |
| Product feeds | `core/tests_regions.py` | `region` / `state` filters, state implies region, case-insensitive, unknown ignored, combined with category / sort / paging, home per-state caching, detail exposes delivery info, cache refresh on region change and on admin exclusion edits |
| Cart delivery info | `core/tests_regions.py` | `delivery` block in shipping-configs, malformed seller ids ignored, query count does not grow with sellers |
| Checkout | `orders/tests_regions.py` | Blocked region / excluded state (saved and one-time address), unrecognised state only blocks when exclusions exist, PIN-state mismatch rejected, saved address uses its own PIN, regional fee vs slab vs ₹0, multi-seller fees and blocked-seller message |
| Category rules | `sellers/tests_category_regions.py` | Seller + superadmin API (rules set / replace / clear / keep, all-region rule dropped, invalid shapes incl. missing ids, cache refresh), access (staff seller 403 on admin endpoint, anonymous 401, unknown profile 404), feeds hide restricted categories only where blocked, rules can't widen store regions, multi-category products, other sellers unaffected, product detail and cart `product_regions`, checkout blocks / allows |
| Location helpers | `src/utils/deliveryRegion.test.js` | State matching, saved selection (corrupt / blocked storage), query params, can-deliver label, location detection incl. denied / offline / outside India |
| Cart rules | `src/utils/cartFinancials.test.js` | Region / state blocking, flags re-derived when location changes, never blocks on missing data, regional vs slab vs free fees, multi-seller totals, zone E, stock and quantity rules unchanged |

## Manual end-to-end checklist (phone 375px and desktop ≥1024px)

1. First visit shows the location picker; it can't be dismissed until a state is chosen; focus starts in the dialog and Tab stays inside it.
2. Choosing a state saves it across reloads; the navbar shows "Deliver to …" (desktop) or the "Delivering to … · Change" strip (phone, 44px tall).
3. Reopening the picker preselects the state; Escape closes it and focus returns to the opener.
4. Shop, home and search only show sellers delivering to the state; changing state resets to page 1.
5. A product from a non-delivering seller shows "doesn't deliver to <state>" and disabled buy buttons.
6. Adding such a product from a store page: the cart flags it but still allows checkout.
7. Checkout: address state drives availability and fees; a PIN from another state shows an inline error and blocks Place Order; a different state from the navbar shows a notice with "Use … as my location".
8. Editing a saved address at checkout switches to a one-time address with the "Save this address" option.
9. Seller dashboard: untick a region or a state, see "Unsaved changes"; either save button persists it; unticking everything is refused.
10. Category restrictions: limit a category (e.g. Aquatic Plants → South) — buyers elsewhere stop seeing those products but still see the seller's other categories; the product page says "doesn't deliver this product to …"; the cart flags only that item.
11. Super admin → Sellers → Edit: the same panel shows the seller's rules; changes save with "Save Changes" and the seller sees them.

## Known gaps (not covered / not built)

- Store pages and the wishlist list products without a "doesn't deliver" note (the cart and checkout still catch them).
- PIN-to-state matching works on 2-digit postal circles, so it can't tell neighbouring states inside one circle apart (e.g. 16xxxx: Punjab or Chandigarh).
- "Use my current location" was tested with stubbed browser location only, not a real permission prompt on a device.
- No automated browser (end-to-end) tests — the checklist above is run by hand.
