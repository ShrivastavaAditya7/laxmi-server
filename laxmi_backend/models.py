"""
Laxmi Textiles — core Django models.
Design rules encoded here (do not violate elsewhere in the codebase):
  1. Stock is NEVER set directly. Only InventoryLedger rows are created;
     InventoryBalance is a derived, reconciled cache.
  2. Every SKU carries exactly one unit_type. Units are never mixed within a SKU.
  3. Cost price is only ever exposed through admin-scoped serializers/permissions.
"""
import uuid
from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Users & roles
# ---------------------------------------------------------------------------
class Role(models.TextChoices):
    ADMIN = "ADMIN", "Admin / Owner"
    BILLING = "BILLING", "Billing Counter"
    WAREHOUSE = "WAREHOUSE", "Warehouse Staff"
    PACKAGING = "PACKAGING", "Packaging / Dispatch"


class StaffUser(TimeStamped):
    """Extends the auth user with a role and a short numeric PIN for
    fast, low-literacy login on shared counter devices."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profile")
    full_name = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.BILLING)
    pin_hash = models.CharField(max_length=255)  # store hashed, never plaintext
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.full_name} ({self.role})"


# ---------------------------------------------------------------------------
# Catalog: Item Type -> Brand/Company -> Product -> SKU/Variant (colour)
# ---------------------------------------------------------------------------
class ItemType(TimeStamped):
    """Top-level category, e.g. Saree, Suiting Fabric, Kurta, Bedsheet Set."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)
    default_unit_type = models.CharField(
        max_length=20,
        choices=[("metre", "Metre"), ("piece", "Piece"), ("set", "Set"),
                  ("dozen", "Dozen"), ("kg", "Kilogram")],
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Brand(TimeStamped):
    """A company/brand as it exists within a given item type, e.g.
    'Raymond' under Suiting Fabric, 'Nalli Silks' under Saree."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_type = models.ForeignKey(ItemType, on_delete=models.PROTECT, related_name="brands")
    name = models.CharField(max_length=120)
    fabric = models.CharField(max_length=80, blank=True)  # e.g. "Wool Blend", "Kanjeevaram Silk"

    class Meta:
        unique_together = ("item_type", "name")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} · {self.item_type.name}"


class Product(TimeStamped):
    """A sellable design/pattern under a brand — the parent of colour variants."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, related_name="products")
    name = models.CharField(max_length=160, blank=True)
    hsn_code = models.CharField(max_length=15, blank=True)
    gst_rate = models.DecimalField(max_digits=4, decimal_places=2, default=5.00)

    def __str__(self):
        return self.name or f"{self.brand.name} product"


class SKU(TimeStamped):
    """The actual stock-keeping unit: one colour/variant of one product.
    This is the row every ledger entry, price, and stock count refers to."""
    UNIT_CHOICES = [("metre", "Metre"), ("piece", "Piece"), ("set", "Set"),
                     ("dozen", "Dozen"), ("kg", "Kilogram")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="skus")
    sku_code = models.CharField(max_length=64, unique=True, db_index=True)
    barcode = models.CharField(max_length=64, unique=True, db_index=True)
    color = models.CharField(max_length=60)
    unit_type = models.CharField(max_length=20, choices=UNIT_CHOICES)

    cost_price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    mrp = models.DecimalField(max_digits=10, decimal_places=2)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    dynamic_price = models.DecimalField(max_digits=10, decimal_places=2)  # kept in sync by pricing engine

    safety_stock_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=6)
    rack = models.CharField(max_length=20, blank=True)  # e.g. "A-3"

    # For metre-based goods cut from a mill roll — enables remnant tracking.
    parent_roll_id = models.CharField(max_length=40, blank=True, null=True)

    is_online_enabled = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["sku_code"]), models.Index(fields=["barcode"])]

    def __str__(self):
        return self.sku_code

    def generate_code(self):
        """Standardized SKU naming: BRAND-FABRIC-COLOUR-BATCH."""
        def short(s, n=3):
            return "".join(c for c in s if c.isalpha())[:n].upper()
        brand = self.product.brand
        return f"{short(brand.name)}-{short(brand.fabric)}-{short(self.color)}"


# ---------------------------------------------------------------------------
# Inventory ledger (source of truth) + derived balance cache
# ---------------------------------------------------------------------------
class LedgerEventType(models.TextChoices):
    OPENING = "OPENING", "Opening stock"
    PURCHASE_RECEIVE = "PURCHASE_RECEIVE", "Purchase received"
    PURCHASE_REJECT = "PURCHASE_REJECT", "Purchase rejected (RCV)"
    SALE_COUNTER = "SALE_COUNTER", "Sale — counter"
    SALE_ONLINE = "SALE_ONLINE", "Sale — online"
    CUT_FROM_ROLL = "CUT_FROM_ROLL", "Cut from roll (remnant created)"
    CUSTOMER_RETURN = "CUSTOMER_RETURN", "Customer return"
    DAMAGE = "DAMAGE", "Damage / rejection"
    RTV = "RTV", "Return to vendor"
    ONLINE_RESERVE = "ONLINE_RESERVE", "Reserved for online order"
    ONLINE_RELEASE = "ONLINE_RELEASE", "Online reservation released"
    STOCK_COUNT_ADJUSTMENT = "STOCK_COUNT_ADJUSTMENT", "Physical count adjustment"
    STOCK_TRANSFER = "STOCK_TRANSFER", "Transfer between locations"


class InventoryLedger(models.Model):
    """Immutable. Never update or delete a row — post a correcting entry instead."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name="ledger_entries")
    event_type = models.CharField(max_length=30, choices=LedgerEventType.choices)
    quantity_delta = models.DecimalField(max_digits=12, decimal_places=3)  # +in / -out, supports fractional metres
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    reference = models.CharField(max_length=64, blank=True)  # bill id, PO id, etc.
    actor = models.ForeignKey(StaffUser, on_delete=models.SET_NULL, null=True, related_name="ledger_entries")
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["sku", "created_at"])]

    def __str__(self):
        return f"{self.sku.sku_code} {self.event_type} {self.quantity_delta}"


class InventoryBalance(models.Model):
    """Fast-read cache, reconciled against InventoryLedger. Never authoritative on its own —
    a nightly job asserts balance == sum(ledger.quantity_delta) and logs/repairs drift."""
    sku = models.OneToOneField(SKU, on_delete=models.CASCADE, primary_key=True, related_name="balance")
    physical_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    reserved_online = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    damaged_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    last_sale_at = models.DateTimeField(null=True, blank=True)
    last_purchase_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def atp(self):
        """Available to Promise."""
        return max(
            self.physical_stock - self.reserved_online - self.damaged_qty,
            0,
        )


# ---------------------------------------------------------------------------
# Suppliers & purchasing (kept minimal here — expand when the purchase module is built)
# ---------------------------------------------------------------------------
class Supplier(TimeStamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    contact_phone = models.CharField(max_length=20, blank=True)
    lead_time_days = models.PositiveIntegerField(default=5)

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
class Channel(models.TextChoices):
    IN_STORE = "IN_STORE", "In-store"
    ECOMMERCE = "ECOMMERCE", "E-commerce"


class PaymentMode(models.TextChoices):
    CASH = "CASH", "Cash"
    UPI = "UPI", "UPI"
    CARD = "CARD", "Card"


class Bill(TimeStamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bill_number = models.CharField(max_length=20, unique=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.IN_STORE)
    cashier = models.ForeignKey(StaffUser, on_delete=models.PROTECT, related_name="bills")
    payment_mode = models.CharField(max_length=20, choices=PaymentMode.choices)
    taxable_value = models.DecimalField(max_digits=12, decimal_places=2)
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return self.bill_number


class BillItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="items")
    sku = models.ForeignKey(SKU, on_delete=models.PROTECT, related_name="bill_items")
    quantity = models.DecimalField(max_digits=10, decimal_places=3)  # supports 2.75 metres
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    unit_cost_snapshot = models.DecimalField(max_digits=10, decimal_places=2)  # for margin reporting

    @property
    def line_total(self):
        return self.quantity * self.unit_price
