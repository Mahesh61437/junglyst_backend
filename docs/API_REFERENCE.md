# Junglyst API Reference

REST API for the Junglyst botanical marketplace — plants, aquatic specimens, and accessories. This reference covers the **user-facing** endpoints consumed by the web frontend: public catalog browsing, buyer auth/cart/checkout/orders, and the seller onboarding + fulfilment workflow. Admin and webhook endpoints are excluded.

> Interactive docs: `/api/docs/` (Swagger UI) and `/api/redoc/` (ReDoc). Raw OpenAPI: `/api/schema/`.

---

## Table of contents

- [Conventions](#conventions)
- [Authentication](#authentication)
- [Errors](#errors)
- [Rate limits](#rate-limits)
- [Pagination](#pagination)
- [Endpoints](#endpoints)
  - [Auth](#auth)
  - [Products](#products)
  - [Reviews](#reviews)
  - [Categories](#categories)
  - [Wishlist](#wishlist)
  - [Cart](#cart)
  - [Checkout & payments](#checkout--payments)
  - [Orders (buyer)](#orders-buyer)
  - [Orders (seller)](#orders-seller)
  - [Shipping addresses](#shipping-addresses)
  - [Shipping rates & serviceability](#shipping-rates--serviceability)
  - [Sellers — public](#sellers--public)
  - [Sellers — dashboard](#sellers--dashboard)
  - [Notifications](#notifications)
  - [Misc (home, config, bug reports, competition)](#misc-home-config-bug-reports-competition)

---

## Conventions

**Base URL**

| Environment | URL |
|---|---|
| Local | `http://127.0.0.1:8000` |
| Production | `https://api.junglyst.com` |

All endpoints are prefixed with `/api/`. Content is JSON unless noted (image uploads use `multipart/form-data`).

**IDs.** Products, orders, sub-orders, users, and most domain objects use UUIDs. Categories, subcategories, addresses, and blackouts use integer PKs.

**Pricing.** `ProductVariant.price` is auto-computed: `base_price + (base_price × gst_rate%) + (base_price × commission_rate%)`. Clients display this value as-is; GST and commission are not separately surfaced on the storefront.

**Timestamps.** ISO 8601 in UTC unless the field is a calendar date.

---

## Authentication

JWT via `djangorestframework-simplejwt`. Obtain tokens at `POST /api/core/login/`, refresh at `POST /api/core/refresh/`.

Send the access token on every authenticated request:

```
Authorization: Bearer <access_token>
```

| Lifetime | Value |
|---|---|
| Access token | 1 day |
| Refresh token | 7 days |
| Rotation | Refresh tokens are rotated; the old one is blacklisted on rotation |

When the access token expires, calling any authenticated endpoint returns `401 Unauthorized` with `{ "detail": "Given token not valid for any token type", ... }`. The frontend's Axios interceptor handles this by redirecting to `/login?expired=true`.

Most read endpoints are public (`AllowAny`). Cart endpoints accept both authenticated requests and anonymous requests with a `session_id` query parameter for guest carts.

---

## Errors

DRF default error shape:

```json
{ "detail": "Authentication credentials were not provided." }
```

Or, for serializer validation errors, a field-keyed map:

```json
{
  "email": ["This field is required."],
  "password": ["Password too short."]
}
```

| Status | Meaning |
|---|---|
| `400` | Bad request / validation error |
| `401` | Missing or invalid JWT |
| `403` | Authenticated but not allowed (e.g. seller-only endpoint, ownership) |
| `404` | Object not found or filtered out by permissions |
| `409` | Conflict (e.g. duplicate seller pickup registration) |
| `429` | Rate-limited (see below) |
| `5xx` | Server error |

---

## Rate limits

DRF throttles are enforced globally:

| Scope | Limit |
|---|---|
| `anon` | 200 req/min per IP |
| `user` | 600 req/min per user |
| `auth` | 10 req/min — applied to login, register, password reset (brute-force protection) |

`POST /api/core/forgot-password/` additionally enforces a 60-second cooldown per email.

---

## Pagination

List endpoints use a page-number paginator (`page_size=20` default):

```
GET /api/core/products/?page=2&page_size=40
```

Response:

```json
{
  "count": 312,
  "next": "http://.../api/core/products/?page=3",
  "previous": "http://.../api/core/products/?page=1",
  "results": [ /* ... */ ]
}
```

---

## Endpoints

### Auth

#### `POST /api/core/register/` — Register

Public. Creates a new user. Role defaults to `collector` (buyer). Sellers must additionally be on the approved-email list (`POST /api/sellers/check-email/`) before they can fully onboard.

**Request**
```json
{
  "email": "asha@example.com",
  "username": "asha",
  "password": "a-strong-passphrase",
  "phone": "+91-9000000000",
  "role": "collector",
  "first_name": "Asha",
  "last_name": "Rao"
}
```

**Response 201**
```json
{
  "user": {
    "id": "f7b1...uuid",
    "email": "asha@example.com",
    "username": "asha",
    "phone": "+91-9000000000",
    "role": "collector",
    "full_name": "Asha Rao",
    "avatar_url": null
  },
  "message": "Registration successful"
}
```

#### `POST /api/core/login/` — Obtain JWT

Public. Accepts either `email` or `username` plus `password`.

**Request**
```json
{ "email": "asha@example.com", "password": "a-strong-passphrase" }
```

**Response 200**
```json
{
  "access": "eyJhbGciOi...",
  "refresh": "eyJhbGciOi...",
  "user": {
    "id": "f7b1...uuid",
    "email": "asha@example.com",
    "username": "asha",
    "role": "collector",
    "is_verified_seller": false,
    "full_name": "Asha Rao",
    "seller_profile": null
  }
}
```

#### `POST /api/core/refresh/` — Refresh access token

Public. Returns a new `access` (and rotated `refresh`).

```json
{ "refresh": "<refresh_token>" }
```

#### `POST /api/core/forgot-password/` — Request reset OTP

Public. Sends a one-time code to the registered email. Cooldown: 60s per email.

```json
{ "email": "asha@example.com" }
```

#### `POST /api/core/reset-password/` — Complete password reset

Public.

```json
{
  "email": "asha@example.com",
  "otp": "493817",
  "new_password": "a-new-strong-passphrase"
}
```

#### `GET /api/core/me/` — Current user

Auth required. Returns the full profile of the logged-in user.

#### `PATCH /api/core/me/` — Update current user

Auth required. Partial-update any of `email`, `username`, `phone`, `first_name`, `last_name`, `avatar_url`, `location`.

#### `POST /api/core/sync-cart/` — Sync guest cart on login

Auth required. Posts a guest cart's items to the user's server-side cart after login.

```json
{
  "items": [
    { "product_id": "uuid", "variant_id": "uuid", "quantity": 2 }
  ]
}
```

---

### Products

#### `GET /api/core/products/` — List products

Public, paginated.

**Query parameters**

| Param | Type | Notes |
|---|---|---|
| `categories` | int/csv | Filter by category id |
| `sub_categories` | int/csv | Filter by subcategory id |
| `seller` | int | Filter by seller profile id |
| `seller_slug` | string | Filter by seller's store slug |
| `is_active` | bool | Default `true` |
| `is_rare` | bool | |
| `in_stock` | bool | |
| `stock_lt` | int | Stock less-than |
| `min_price`, `max_price` | decimal | |
| `care_level` | string | `easy`, `medium`, `hard` |
| `search` | string | Searches name + description |
| `ordering` | string | e.g. `-created_at`, `price`, `-price` |
| `page`, `page_size` | int | Pagination |

#### `GET /api/core/products/id/{uuid}/` — Product detail by ID

Public.

#### `GET /api/core/products/{slug}/` — Product detail by slug

Public. Returns the same shape as the ID variant.

**Response (abridged)**
```json
{
  "id": "uuid",
  "slug": "anubias-nana-petite",
  "name": "Anubias Nana Petite",
  "scientific_name": "Anubias barteri var. nana 'Petite'",
  "description": "Compact rhizome species ...",
  "care_level": "easy",
  "light_requirements": "low-medium",
  "growth_rate": "slow",
  "water_temperature": "22-28C",
  "ph_range": "6.0-7.5",
  "origin": "West Africa",
  "is_rare": false,
  "rating": 4.7,
  "seller": { "id": 12, "store_name": "Lush Aquatics", "slug": "lush-aquatics" },
  "categories": [ { "id": 3, "name": "Aquatic plants" } ],
  "sub_categories": [ { "id": 11, "name": "Rhizome" } ],
  "variants": [
    { "id": "uuid", "name": "Tissue-cultured cup", "price": "499.00", "stock": 14 }
  ],
  "images": [ { "id": 1, "url": "https://..." } ]
}
```

#### `PATCH /api/core/products/id/{uuid}/` — Update product

Auth required. Owner (seller) or admin only. Partial updates of any subset of fields including nested `variants` and `images`.

#### `DELETE /api/core/products/id/{uuid}/` — Archive product

Auth required. Soft-delete (sets `is_active=false`). Owner or admin.

#### `POST /api/core/products/create/` — Create product (seller)

Auth required. `role=grower` (seller). Required: `name`, `description`, `category_id`, `variants[]`.

```json
{
  "name": "Bucephalandra Brownie Ghost",
  "description": "Slow-growing rhizome ...",
  "category_id": 3,
  "sub_category_ids": [11],
  "variants": [
    { "name": "Cup", "base_price": "400.00", "stock": 5 }
  ],
  "images": [ { "url": "https://..." } ]
}
```

`price` on each variant is computed server-side from `base_price` + GST + commission.

#### `POST /api/core/products/bulk-action/` — Bulk action

Auth required. Seller (own products) or admin.

```json
{ "action": "publish", "ids": ["uuid", "uuid"] }
```

`action` ∈ `publish`, `archive`, `unarchive`, `delete`.

#### `POST /api/core/products/bulk-stock-update/` — Bulk stock update

Auth required. Seller (own variants) or admin.

```json
{
  "updates": [
    { "variant_id": "uuid", "stock": 12 },
    { "variant_id": "uuid", "stock": 0 }
  ]
}
```

---

### Reviews

#### `GET /api/core/reviews/` — List reviews

Public.

| Param | Notes |
|---|---|
| `productId` | Required — filter by product UUID |

#### `POST /api/core/reviews/` — Create review

Auth required.

```json
{
  "product_id": "uuid",
  "author": "Asha R.",
  "comment": "Arrived healthy, well packed.",
  "plants": 5,
  "packaging": 5,
  "responsiveness": 4,
  "image": "<multipart upload, optional>"
}
```

---

### Categories

#### `GET /api/core/categories/` — List categories

Public. Returns categories with nested subcategories, shipping rates, GST and commission percentages.

#### `GET /api/core/subcategories/` — List subcategories

Public.

| Param | Notes |
|---|---|
| `category` | Filter by parent category id |

(Create/update/delete on categories and subcategories is admin-only and not documented here.)

---

### Wishlist

Wishlist is per-user.

#### `GET /api/core/wishlist/` — List wishlist

Auth required.

#### `POST /api/core/wishlist/` — Toggle add/remove

Auth required.

```json
{ "product_id": "uuid" }
```

**Response**
```json
{ "status": "added", "product_id": "uuid" }
```

`status` is `"added"` or `"removed"` depending on prior state.

#### `DELETE /api/core/wishlist/{product_id}/` — Remove

Auth required.

---

### Cart

Cart works for both authenticated users and anonymous guests. Guests pass a `session_id` query parameter on every request; authenticated users have their cart resolved by JWT.

#### `GET /api/cart/` — Get cart

| Param | Notes |
|---|---|
| `session_id` | Required for guests, ignored for auth users |

**Response**
```json
{
  "id": "uuid",
  "items": [
    {
      "id": "uuid",
      "product": { "id": "uuid", "name": "Anubias Nana", "image_url": "https://..." },
      "variant": { "id": "uuid", "name": "Cup", "price": "499.00" },
      "quantity": 2,
      "subtotal": "998.00"
    }
  ],
  "total_items": 2,
  "subtotal": "998.00"
}
```

#### `POST /api/cart/add_item/` — Add item

```json
{ "variant_id": "uuid", "quantity": 1, "session_id": "guest-uuid-or-null" }
```

#### `POST /api/cart/remove_item/` — Remove item

```json
{ "cart_item_id": "uuid", "session_id": "guest-uuid-or-null" }
```

#### `PATCH /api/cart/{id}/update_item/` — Update quantity

```json
{ "quantity": 3 }
```

#### `POST /api/cart/clear/` — Clear cart

| Param | Notes |
|---|---|
| `session_id` | Required for guests |

---

### Checkout & payments

The checkout flow is: (1) `POST /checkout/` to create an order + payment session, (2) the client redirects to or invokes Cashfree/Razorpay SDK, (3) on return, `POST /checkout/verify/` confirms the payment, and (4) optionally `GET /payment-status/` polls for the gateway's final state.

#### `POST /api/orders/checkout/` — Create order from cart

Public. Anonymous guests pass `guest_info`; authenticated users pass `address_id` (or `guest_info` if no saved address).

```json
{
  "cart_id": "uuid",
  "address_id": 17,
  "pincode": "560001",
  "guest_info": {
    "email": "guest@example.com",
    "phone": "+91-9000000000",
    "address": "..."
  }
}
```

**Response 201**
```json
{
  "order_id": "uuid",
  "order_number": "JNG-202605-0001",
  "amount": "1248.00",
  "currency": "INR",
  "cashfree_session_id": "session_xxxxx",
  "razorpay_order_id": null
}
```

Exactly one of `cashfree_session_id` / `razorpay_order_id` is populated based on which gateway is active.

#### `POST /api/orders/checkout/verify/` — Verify payment

Public. Called after the gateway returns to the client.

```json
{
  "gateway": "razorpay",
  "razorpay_order_id": "order_xxx",
  "razorpay_payment_id": "pay_xxx",
  "razorpay_signature": "..."
}
```

Or, for Cashfree:

```json
{ "gateway": "cashfree", "cashfree_order_id": "order_xxx" }
```

**Response**
```json
{
  "message": "Payment verified",
  "order": { "id": "uuid", "order_number": "JNG-202605-0001", "status": "paid", "total_amount": "1248.00" }
}
```

#### `GET /api/orders/payment-status/` — Poll status

Public.

| Param | Notes |
|---|---|
| `cashfree_order_id` or `razorpay_order_id` | one is required |

**Response**
```json
{ "status": "success", "order_number": "JNG-202605-0001", "order": { /* ... */ } }
```

`status` ∈ `processing`, `success`, `failed`.

#### `GET /api/payments/gateway-settings/` — Active gateway info

Public. Tells the frontend which gateway to use and surfaces the public keys it needs (e.g. Razorpay key id).

**Response**
```json
{ "active": "razorpay", "razorpay_key_id": "rzp_test_xxx" }
```

---

### Orders (buyer)

#### `GET /api/orders/` — List my orders

Auth required. Paginated, newest first.

#### `GET /api/orders/{uuid}/` — Order detail

Auth required. Owner only.

**Response (abridged)**
```json
{
  "id": "uuid",
  "order_number": "JNG-202605-0001",
  "status": "paid",
  "payment_status": "paid",
  "subtotal": "998.00",
  "shipping_fee": "250.00",
  "total_amount": "1248.00",
  "created_at": "2026-05-28T14:00:00Z",
  "items": [
    { "id": "uuid", "product_name": "Anubias Nana", "variant_name": "Cup", "unit_price": "499.00", "quantity": 2 }
  ],
  "sub_orders": [
    { "id": "uuid", "sub_order_number": "...", "seller_name": "Lush Aquatics", "status": "confirmed", "tracking_status": "pending_pickup" }
  ]
}
```

#### `GET /api/orders/track/` — Track by order number

Public.

| Param | Notes |
|---|---|
| `order_number` | required |

#### `POST /api/orders/{uuid}/cancel/` — Cancel order

Auth required. Owner only. Only `pending` / `paid` orders that have not been shipped can be cancelled.

---

### Orders (seller)

Each `Order` produced by checkout fans out to one `SubOrder` per seller. The fulfilment workflow operates on sub-orders.

#### `GET /api/orders/seller/` — List my sub-orders

Auth required. Seller role. Paginated.

#### `GET /api/orders/seller/sub-orders/` — List with filters

Auth required.

| Param | Notes |
|---|---|
| `status` | `pending`, `confirmed`, `packed`, `shipped`, `delivered`, `cancelled` |
| `order_number` | Filter by parent order |

#### `GET /api/orders/seller/sub-orders/{uuid}/` — Sub-order detail

Auth required. Seller (owner) only.

#### `POST /api/orders/seller/sub-orders/{uuid}/confirm/` — Confirm

Auth required. Seller has a 48-hour window to confirm a paid sub-order; this transitions status `pending → confirmed` and starts the dispatch deadline timer.

#### `POST /api/orders/seller/sub-orders/{uuid}/upload-photo/` — Upload packaging photo

Auth required. `multipart/form-data` with field `image`. Required before shipping live specimens.

**Response**
```json
{ "package_image_url": "https://..." }
```

#### `PATCH /api/orders/seller/sub-orders/{uuid}/shipment-details/` — Set AWB

Auth required.

```json
{ "awb_number": "AWB123456", "courier_name": "Bluedart" }
```

#### `POST /api/orders/seller/sub-orders/{uuid}/ship/` — Mark as shipped

Auth required. Requires packaging photo and shipment details to already be set.

#### `PATCH /api/orders/seller/sub-orders/{uuid}/status/` — Update status

Auth required. Manual override of sub-order status, e.g. to mark `delivered` if the courier webhook didn't fire.

```json
{ "status": "delivered" }
```

#### `POST /api/orders/ship-now/` — Bulk ship

Auth required. Ships multiple sub-orders in one call.

```json
{ "sub_order_ids": ["uuid", "uuid"] }
```

---

### Shipping addresses

ViewSet — standard REST. Each user can save up to 5 addresses.

| Method | Path |
|---|---|
| `GET` | `/api/shipping/addresses/` |
| `POST` | `/api/shipping/addresses/` |
| `GET` | `/api/shipping/addresses/{id}/` |
| `PATCH` | `/api/shipping/addresses/{id}/` |
| `DELETE` | `/api/shipping/addresses/{id}/` |

**Create body**
```json
{
  "full_name": "Asha Rao",
  "phone": "+91-9000000000",
  "address_line_1": "12 Indiranagar 1st Cross",
  "address_line_2": "Apt 4B",
  "city": "Bengaluru",
  "state": "Karnataka",
  "pincode": "560038",
  "is_default": true
}
```

Setting `is_default=true` on one address unsets the others.

---

### Shipping rates & serviceability

#### `POST /api/shipping/logistics/` — Rate quote

Public. Returns courier options and rates for a parcel.

```json
{
  "origin_pincode": "560001",
  "destination_pincode": "110001",
  "weight": 0.6,
  "length": 20, "breadth": 15, "height": 10,
  "order_value": 1000
}
```

**Response**
```json
{
  "status": "serviceable",
  "couriers": [
    { "name": "Bluedart Air", "rate_total": "180.00", "delivery_days": 2 },
    { "name": "Delhivery Surface", "rate_total": "95.00", "delivery_days": 4 }
  ]
}
```

#### `GET /api/shipping/logistics/` — List configured providers

Public.

#### `POST /api/shipping/pincode-check/` — Check deliverability

Public.

```json
{ "pincode": "682001" }
```

**Response**
```json
{ "is_deliverable": true, "zone": "B" }
```

#### `POST /api/shipping/package-image/` — Upload package image

Auth required. Seller. `multipart/form-data` field `image`. Used for the packaging photo on a sub-order.

---

### Sellers — public

#### `GET /api/sellers/` — List active sellers

Public. Paginated.

| Param | Notes |
|---|---|
| `featured` | `true` to only return `is_featured=true` sellers (in `sort_order`) |

#### `GET /api/sellers/store/{slug}/` — Public storefront

Public. Returns brand/profile data for a seller's storefront page.

**Response**
```json
{
  "store_name": "Lush Aquatics",
  "slug": "lush-aquatics",
  "tagline": "Tissue-cultured rhizomes from the source",
  "bio": "...",
  "logo_url": "https://...",
  "banner_url": "https://...",
  "icon_url": "https://...",
  "brand_color": "#1f6f4a",
  "location_city": "Cochin",
  "rating": 4.8,
  "total_sales": 1842,
  "expertise_tags": ["aquatic", "rhizome"],
  "experience_years": 7,
  "identity_verified": true
}
```

#### `GET /api/sellers/platform-stats/` — Platform totals

Public. Returns `total_sellers`, `total_products`, `total_users`. Used on the home page.

#### `GET /api/sellers/featured-curator/` — Featured seller of the day

Public. Returns one randomly-selected featured seller profile.

#### `POST /api/sellers/check-email/` — Email allowlist check

Public. Used during signup to tell the user whether their email is approved to become a seller.

```json
{ "email": "newseller@example.com" }
```

**Response**
```json
{ "is_allowed": true }
```

---

### Sellers — dashboard

All routes require authentication. Most require `role=grower`.

#### `GET /api/sellers/dashboard/` — Dashboard data

Returns metrics (revenue, orders, top products, sales chart, low-stock, inventory distribution, recent activity) plus the seller's own profile.

#### `POST /api/sellers/dashboard/` — Initialize seller profile

Used during onboarding after `check-email` confirms the user is allowlisted. Body is the full set of profile fields (`store_name`, `bio`, `location_city`, etc.).

#### `GET /api/sellers/check-approval/` — Is the logged-in user an approved seller?

Returns `{ is_approved, email_checked }`.

#### `GET /api/sellers/bank-details/` — Get bank details

Returns masked account info.

#### `POST /api/sellers/bank-details/` — Save bank details

```json
{
  "payout_type": "bank",
  "payout_account": "1234567890",
  "ifsc_code": "HDFC0001234",
  "gst_number": "29ABCDE1234F1Z5"
}
```

#### `GET /api/sellers/pickup-address/` — Get pickup address + Shiprocket status

Returns the seller's pickup address and the registration status with Shiprocket.

#### `PATCH /api/sellers/pickup-address/` — Update pickup address

Updates the address and auto-registers with Shiprocket. Set `reset_shiprocket_location=true` to force re-registration if the address changed.

#### `POST /api/sellers/pickup-address/register/` — Retry Shiprocket registration

Re-attempts registration without changing the address (useful after failure).

#### `POST /api/sellers/pickup-address/otp/` — Verify Shiprocket OTP

```json
{ "otp": "493817" }
```

#### `GET /api/sellers/blackouts/` — List blackout dates

Days the seller is unavailable to ship.

#### `POST /api/sellers/blackouts/` — Add blackout window

```json
{ "start_date": "2026-06-10", "end_date": "2026-06-14", "reason": "Travel" }
```

#### `DELETE /api/sellers/blackouts/{id}/` — Remove blackout

#### `GET /api/analytics/seller/gst-invoice/` — Seller GST report

| Param | Notes |
|---|---|
| `start_date`, `end_date` | ISO date range |

Returns the seller's GST-relevant invoice rollup for the period.

---

### Notifications

#### `GET /api/notifications/` — List

Auth required. Paginated, newest first.

#### `POST /api/notifications/mark-read/` — Mark read

Auth required. Pass `{ "id": "<notification-id>" }` to mark a single notification read, or no body to mark all read.

#### `GET /api/notifications/unread-count/` — Unread count

Auth required. Cached for 30 seconds per user.

#### `POST /api/notifications/newsletter/subscribe/` — Newsletter signup

Public.

```json
{ "email": "newsletter@example.com" }
```

#### `POST /api/notifications/contact/` — Contact form

Public.

```json
{
  "name": "Asha Rao",
  "email": "asha@example.com",
  "phone": "+91-9000000000",
  "topic": "Order issue",
  "message": "..."
}
```

---

### Misc (home, config, bug reports, competition)

#### `GET /api/core/home/` — Home page aggregate

Public. One round-trip for the homepage — returns featured products, categories, and platform stats together.

#### `GET /api/core/config/public/{name}/` — Public config

Public. Generic key-value config readable to anyone. Used for things like the competition settings blob.

```
GET /api/core/config/public/competition_settings/
```

**Response**
```json
{
  "id": 1,
  "name": "competition_settings",
  "data": { "max_entries": 100, "prize_amount": 25000, "currency": "INR" },
  "created_at": "...",
  "updated_at": "..."
}
```

#### `POST /api/core/upload/` — Upload image

Auth required. `multipart/form-data` field `image`. Returns Firebase Storage URL.

```json
{ "url": "https://firebasestorage.googleapis.com/..." }
```

#### `GET /api/core/bug-reports/` — List my bug reports

Auth required.

#### `POST /api/core/bug-reports/` — Submit bug report

Auth required.

```json
{ "contact_info": "+91-9000000000", "description": "Checkout button greyed out on Safari" }
```

#### `GET /api/core/bug-reports/{uuid}/` — Bug report detail

Auth required. Owner only.

#### `PATCH /api/core/bug-reports/{uuid}/` — Update bug report

Auth required. Owner only.

#### `GET /api/competition/status/` — Competition state

Public. Returns countdown, slot count, and (after results) the winner.

```json
{
  "launch_date": "2026-06-01T10:00:00Z",
  "is_open": true,
  "total_entries": 47,
  "slots_remaining": 53,
  "max_entries": 100,
  "seconds_until_launch": 0,
  "winner_announced": false,
  "winner": null,
  "prize_amount": 25000,
  "prize_currency": "INR",
  "result_announcement_date": "2026-07-01T10:00:00Z"
}
```

#### `POST /api/competition/enter/` — Submit competition entry

Public. `multipart/form-data`.

| Field | Type |
|---|---|
| `name` | string |
| `email` | string |
| `mobile` | string |
| `about_aquarium` | text |
| `images` | one or more file uploads |

**Response**
```json
{ "success": true, "message": "Entry received", "entry_id": "uuid", "name": "Asha Rao", "slots_remaining": 52 }
```

---

## Changelog

- **1.0.0** (2026-05-28) — Initial public reference covering buyer + seller user-facing routes.
