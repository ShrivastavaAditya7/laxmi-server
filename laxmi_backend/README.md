# Laxmi Textiles — Backend (Django + DRF)

## What's here
- `models.py` — catalog (ItemType → Brand → Product → SKU), the immutable
  `InventoryLedger`, the derived `InventoryBalance` cache, billing tables, and roles.
- `services.py` — movement scoring, ABC tiering, dynamic pricing, reorder point math.
  Pure functions, unit-testable without touching the database.
- `permissions.py` — role gates (`IsAdmin`, `IsBillingOrAdmin`, etc.) used on every view.
- `serializers.py` — `SKUSerializer` vs `AdminSKUSerializer`: cost price and margin
  literally do not exist on the JSON payload sent to non-admin roles.
- `views.py` — catalog read endpoints, `StockMovementView` (ledger writes for
  warehouse), `CheckoutView` (atomic POS checkout with row locking), dashboard summary.
- `urls.py` — routes, plus a PIN-based `StaffLoginView` that issues a normal
  DRF auth token underneath.

## Install
```bash
pip install django djangorestframework --break-system-packages
django-admin startproject laxmi_project .
# copy this folder in as an app, e.g. `laxmi_backend`, add to INSTALLED_APPS:
#   'rest_framework', 'rest_framework.authtoken', 'laxmi_backend'
python manage.py makemigrations laxmi_backend
python manage.py migrate
python manage.py createsuperuser   # for Django admin access
python manage.py runserver
```

Add to `settings.py`:
```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authtoken.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
AUTH_USER_MODEL = "auth.User"  # or swap for a custom user model later
```

For each `StaffUser`, set the underlying Django `User`'s password to the PIN
(`user.set_password("4821")`) — PINs are then just short passwords, handled by
Django's normal hashing, nothing custom to secure.

## How this maps to the frontend prototype
| Frontend concept | Backend equivalent |
|---|---|
| `INITIAL_PRODUCTS` catalog | `ItemType` → `Brand` → `Product` → `SKU` rows, seeded via a management command or Django admin/fixtures |
| `atp(p)` | `services.atp_for_balance()` |
| `movementScore(p)` / `tier(score)` | `services.movement_score()` / `tier_for_score()` — same formula, same weights |
| Billing cart → "Close bill" | `POST /api/billing/checkout/` — atomic, row-locked, writes ledger + `Bill`/`BillItem` |
| Warehouse "Receive" / "Cut from roll" | `POST /api/stock/movement/` with `event_type` |
| Admin dashboard KPIs | `GET /api/dashboard/summary/` |
| Role-based screens | `permissions.py` classes gate every endpoint; `AdminSKUSerializer` gates fields |
| Login (role → name → PIN) | `POST /api/auth/login/` returns a token + role |

## Deliberately not built yet (say the word and I'll add these next)
- Purchase orders / supplier module (models stub for `Supplier` exists, PO flow doesn't)
- Nightly reconciliation job asserting `InventoryBalance == sum(ledger)` — should be
  a Celery beat task or a cron-triggered management command
- E-commerce channel sync (webhook receivers for Shopify/WooCommerce orders)
- GST invoice PDF generation
- Real barcode/label printing integration
