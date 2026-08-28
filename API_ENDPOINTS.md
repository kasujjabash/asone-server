# API Endpoints

Every endpoint below belongs to a feature marked **complete**. Endpoints that
exist but whose feature is still partial are listed at the bottom, separately,
so nothing here is mistaken for finished work.

Completed features covered: **F01, F02, F03, F05, F06, F07, F08, F10, F11,
F12, F14, F15**.

Live, always-current version of this: **`/api/docs/`** — generated from the
code, so it cannot go stale the way this file can.

---

## Conventions

**Base URL** — `http://127.0.0.1:8000` in development.

**Authentication** — every endpoint except sign-in requires a bearer token:

```
Authorization: Bearer <access_token>
```

Access tokens last 30 minutes. Refresh tokens last 12 hours and **rotate** —
the one you send is blacklisted, so sending it twice returns 401. That is
correct behaviour, not a bug.

**Sign in with an email address.** There is no username anywhere in this API.

**List responses are paginated**, 50 per page:

```json
{ "count": 32, "next": "...?page=2", "previous": null, "results": [ ... ] }
```

**Money is a string**, never a number — `"25000.00"`. Floating point cannot
represent `0.1` exactly, and a rounding error on a price becomes a wrong
invoice. Parse it with a decimal library, not `parseFloat`.

**Dates are `YYYY-MM-DD`.** Endpoints that read pricing accept `?on=` to ask
"as at this date"; the default is today.

**Errors**

| Code | Meaning |
|---|---|
| 400 | Your input was rejected. The body names the field |
| 401 | No token, or it expired. Sign in or refresh |
| 403 | Signed in, but your role is not allowed to do this |
| 404 | Not found |
| 409 | The row is still in use by something else. Nothing you change about the request will help |
| 429 | Rate limited. Login is 10/min per email address |

A 400 body is keyed by field, with `non_field_errors` for whole-record rules:

```json
{ "non_field_errors": ["This garment already has a price covering part of that period."] }
```

A 409 names what is blocking the delete, so the message can be acted on:

```json
{
  "detail": "This cannot be deleted while other records still refer to it. Still in use by: Namayemba PS.",
  "in_use_by": ["Namayemba PS (Primary School)"]
}
```

---

## Deleting

Master data splits in two, and the line is **does the row carry meaning that
outlives it**.

**Cannot be deleted — 405.** Retire these instead:

| Resource | Retire with | Why |
|---|---|---|
| SKUs | `PATCH {"is_active": false}` | The control number is printed on documents and never reissued |
| Prices | `PATCH` to correct it | A March invoice reprints at March's price only while that row exists |
| Users | `POST /users/{id}/deactivate/` | Sign-in history — and soon every stock movement — points at them |

**Can be deleted** — garments, sizes, sites, minimum stock levels. These are
configuration, and deleting is how a typo gets fixed. The database still
refuses any row that is referenced, with a **409** naming what is using it.

---

## Authentication — F01

### `POST /api/auth/login/`

No token required.

```json
{ "email": "sharon@asone.test", "password": "..." }
```

Returns the tokens **and the whole user**, so no second call is needed to
render the app:

```json
{
  "access": "eyJ...",
  "refresh": "eyJ...",
  "user": {
    "id": 5,
    "email": "julius@asone.test",
    "first_name": "Julius",
    "last_name": "Okello",
    "role": "WAREHOUSE_STAFF",
    "role_display": "Warehouse Staff",
    "warehouse": { "id": 2, "name": "Namayemba" },
    "school": null,
    "is_active": true,
    "must_change_password": false,
    "access": {
      "scope": "assigned_warehouse",
      "functions": {
        "table_updates": false,
        "production_orders": false,
        "warehouse_receiving_and_shipping": true,
        "inventory_adjustments": false,
        "school_orders": false,
        "backorder_transfers": true,
        "financial_reports": false
      }
    }
  }
}
```

> **`must_change_password: true` means route straight to the change-password
> screen.** That account gets 403 on everything except reading itself,
> setting a password, and signing out.

401 on a wrong password **and** on an unknown address — the two responses are
identical, so neither reveals whether an account exists.

### `POST /api/auth/refresh/`

```json
{ "refresh": "eyJ..." }
```

Returns a new `access` **and a new `refresh`**. Store both — the old refresh
token is dead.

### `POST /api/auth/verify/`

```json
{ "token": "eyJ..." }
```

200 if valid, 401 if not.

### `POST /api/auth/logout/`

```json
{ "refresh": "eyJ..." }
```

Returns **205**. Blacklists the refresh token. The access token stays valid
until it expires — inherent to stateless tokens, and why they are short.

---

## Access control — F02

### `GET /api/auth/me/`

The signed-in user, same shape as `login`'s `user` object.

### `GET /api/auth/roles/`

The five roles and the seven columns of AsOne's access matrix.

```json
[
  {
    "value": "WAREHOUSE_STAFF",
    "label": "Warehouse Staff",
    "summary": "One warehouse. Enters receipts from the Tailoring Centers...",
    "caveat": "Cannot see the other warehouse's stock, change master data...",
    "scope": "assigned_warehouse",
    "requires_site": "warehouse",
    "functions": { "...": true }
  }
]
```

> **Do not hardcode roles in the frontend.** Use `requires_site` to decide
> whether a user form shows a warehouse picker, a school picker, or neither,
> and `access.functions` to decide which navigation items to draw.
>
> Hiding a menu is cosmetic. The server re-checks every request, so a 403 is
> always possible and must be handled.

---

## Passwords — F03

### `POST /api/auth/password/change/`

Your own password.

```json
{ "current_password": "...", "new_password": "..." }
```

Requires the current password even though you are signed in, so a stolen
token alone cannot lock the owner out. Signs out every other session and
returns a fresh token pair.

### `POST /api/auth/users/{id}/set-password/`

Someone else's password — Program Lead and Operations Manager only.

```json
{ "new_password": "...", "must_change_password": true }
```

No current password needed; the point is that nobody has it. Signs that
account out everywhere. Omit `new_password` to have one generated and
returned once.

Django's validators apply, so a lead cannot set `1234` for a colleague.

---

## User accounts — F14

**Program Lead and Operations Manager only.** Everyone else gets 403.

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/auth/users/` | List. Filter `?role=` `?is_active=` `?warehouse=` `?school=` |
| `POST` | `/api/auth/users/` | Add a user |
| `GET` | `/api/auth/users/{id}/` | One user |
| `PATCH` | `/api/auth/users/{id}/` | Edit name, email, role, site |
| `POST` | `/api/auth/users/{id}/activate/` | Restore access |
| `POST` | `/api/auth/users/{id}/deactivate/` | Remove access, sign out |
| `POST` | `/api/auth/users/{id}/sign-out/` | Retire sessions, account stays usable |

**Adding a user:**

```json
{
  "first_name": "Miriam",
  "last_name": "Achieng",
  "email": "miriam@asone.test",
  "role": "SCHOOL_STAFF",
  "school": 1,
  "password": "a-strong-passphrase"
}
```

Warehouse staff need `warehouse`; school staff need `school`; the
all-locations roles take neither — check `requires_site` from `/roles/`.
Sending the wrong one is a 400.

The account must replace the password at first sign-in unless you send
`must_change_password: false`.

**Accounts are never deleted** — `DELETE` returns 405. Deactivate instead:
the audit trail and, in time, the stock ledger point at them.

---

## Master data — sites — F10, F11, F12

| Path | Feature |
|---|---|
| `/api/catalog/tailoring-centers/` | F10 |
| `/api/catalog/warehouses/` | F11 |
| `/api/catalog/schools/` | F12 |

Each supports `GET` (list), `POST`, `GET /{id}/`, `PATCH /{id}/`.

```json
{ "id": 2, "name": "Namayemba", "address": "",
  "primary_tailoring_center": 1, "primary_tailoring_center_name": "Idudi" }
```

Schools carry `level` (`PS` or `HS`), `level_display`, and
`primary_warehouse`. Filter with `?level=` and `?primary_warehouse=`.

---

## Master data — products — F05, F06, F07

### Garments and sizes — F05

`/api/catalog/garments/` and `/api/catalog/sizes/`

```json
{ "id": 1, "name": "White Shirt", "school_level": "BOTH",
  "school_level_display": "Both", "colour": "White", "is_active": true,
  "current_price": "25000.00", "sku_count": 5 }
```

`current_price` is **null**, not `0`, when a garment has no price today — a
missing price is a data gap, not a free uniform.

Filter with `?school_level=` and `?is_active=`; search with `?search=`.

### SKUs — F06

`/api/catalog/skus/`

```json
{ "id": 12, "number": "100015", "garment": 3, "garment_name": "Blue Tunic (PS)",
  "size": 2, "size_name": "10", "description": "Blue Tunic Blue size 10 (PS)",
  "is_active": true, "unit_price": "30000.00" }
```

> **`number` is read-only.** It is assigned by the system, never reused, and
> printed on pick lists — sending one on create is ignored.
>
> **SKUs cannot be deleted** (405). Set `is_active: false` to retire one.
>
> **`unit_price` reads through to the garment.** A SKU has no price of its
> own, because price does not vary by size.

Filter `?garment=` `?size=` `?is_active=` `?garment__school_level=`;
search `?search=` over number and description.

### Minimum stock levels — F07

`/api/catalog/minimum-stock-levels/`

```json
{ "id": 1, "sku": 12, "sku_number": "100015",
  "sku_description": "Blue Tunic Blue size 10 (PS)",
  "warehouse": 2, "warehouse_name": "Namayemba", "minimum_quantity": 120 }
```

One row per SKU per warehouse. Filter `?warehouse=` `?sku=`.

---

## Pricing — F08

### `GET /api/catalog/garments/{id}/prices/`

The full price history for a garment, newest first.

### `POST /api/catalog/garments/{id}/reprice/`

**The correct way to change a price.**

```json
{ "unit_price": "28000.00", "active_from": "2027-01-01" }
```

Closes the current open-ended price on that date and opens a new one.
Returns 201 with the new price.

> Repricing **adds history, it never rewrites it**. An invoice raised in
> March still costs out at March's price when reprinted in September.

### `/api/catalog/prices/`

The whole pricing table, for corrections and auditing. `GET`, `POST`,
`PATCH /{id}/`.

```json
{ "id": 1, "garment": 1, "garment_name": "White Shirt",
  "unit_price": "25000.00", "active_date": "2026-01-01",
  "expiration_date": "2027-01-01" }
```

> **`expiration_date` is exclusive** — the first day the price *no longer*
> applies. A price running `2026-01-01 → 2027-01-01` covers all of 2026 and
> none of 2027. This makes consecutive prices meet exactly instead of
> overlapping on the changeover day.

**Prices cannot be deleted** (405). Correct a mistaken row with `PATCH` —
deleting one would silently rewrite what a past invoice reprints at.

Two prices covering the same day for one garment are **impossible** — the
database refuses them. You get a 400:

```json
{ "non_field_errors": ["This garment already has a price covering part of that period. Close the existing price first."] }
```

---

## Price lists — F15

### `GET /api/catalog/price-lists/?level=PS`

`level` is `PS` or `HS` and is required. Add `?on=YYYY-MM-DD` for a past or
future date.

```json
[
  { "garment_id": 3, "garment": "Blue Tunic (PS)", "colour": "Blue", "unit_price": "30000.00" },
  { "garment_id": 7, "garment": "Socks", "colour": "White", "unit_price": "5000.00" }
]
```

Garments marked `BOTH` appear on each list.

> **Unpriced garments are omitted, not shown at zero.** A price list is a
> document a school orders from; a line with no price is worse than no line.

### `GET /api/catalog/price-lists/gaps/`

Active garments with **no** price on that date — the report to run before
publishing a list, or a garment silently disappears from what schools can
order. Finance and leads only.

---

## Who can do what

Editing master data is the "Table Updates" column of AsOne's matrix — leads
only. Reading is granted table by table, and not to the same roles each time.

| Endpoint | Read | Write |
|---|---|---|
| `/auth/users/` | leads | leads |
| `/auth/roles/`, `/auth/me/` | anyone signed in | — |
| `/catalog/garments/`, `/sizes/`, `/skus/` | leads, warehouse, school, finance | leads |
| `/catalog/prices/` | leads, school, finance | leads |
| `/catalog/minimum-stock-levels/` | leads, warehouse | leads |
| `/catalog/tailoring-centers/`, `/warehouses/` | leads, warehouse | leads |
| `/catalog/schools/` | leads, warehouse, school | leads |
| `/catalog/price-lists/` | leads, warehouse, school, finance | — |
| `/catalog/price-lists/gaps/` | leads, finance | — |

"Leads" = Program Lead and Operations Manager.

Note **warehouse staff cannot read prices**, and **school staff cannot read
minimum stock levels** — those cells are blank in the client's matrix.

---

## Utility

| Path | Purpose |
|---|---|
| `GET /api/` | Directory of the API |
| `GET /api/docs/` | Interactive documentation |
| `GET /api/schema/` | OpenAPI document, for generating a client |
| `/admin/` | Django admin, used by Central Office |

---

## Exists but NOT counted as complete

These work, but their feature is still partial. Do not mark them done.

| Endpoint | Feature | Why it is not complete |
|---|---|---|
| `PATCH /api/auth/me/` | F63 User settings screen | Backend works; there is no screen |
| `GET /api/auth/login-attempts/` | F04 User on every transaction | Sign-ins are audited. "Every stock movement and order action" needs the inventory ledger, which does not exist yet |

---

## Not built at all

`inventory`, `procurement` and `orders` have no endpoints. That covers Uniform
Kits (F09), reason codes (F13), group and production orders (F16–F18),
receipts (F19–F22), all inventory adjustments (F23–F28), the whole point of
sale and fulfilment (F29–F46), and every report that needs stock levels
(F47–F50, F52–F58).
