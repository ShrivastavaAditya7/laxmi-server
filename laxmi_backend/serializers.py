"""
Serializers enforce field-level RBAC: cost price and margin figures are only ever
included for Admin. This is deliberate — hiding a field in the frontend is not
enough, since a curious staff member could still hit the API directly.
"""
from rest_framework import serializers
from .models import (
    ItemType, Brand, Product, SKU, InventoryLedger, InventoryBalance,
    Bill, BillItem, StaffUser, Role,
)
from .services import movement_score, tier_for_score, atp_for_balance, suggested_price


class ItemTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ItemType
        fields = ["id", "name", "default_unit_type"]


class BrandSerializer(serializers.ModelSerializer):
    item_type_name = serializers.CharField(source="item_type.name", read_only=True)

    class Meta:
        model = Brand
        fields = ["id", "item_type", "item_type_name", "name", "fabric"]


class SKUSerializer(serializers.ModelSerializer):
    """Base serializer — safe for any authenticated role.
    Cost price and margin are added only by AdminSKUSerializer below."""
    brand_name = serializers.CharField(source="product.brand.name", read_only=True)
    item_type = serializers.CharField(source="product.brand.item_type.name", read_only=True)
    physical_stock = serializers.SerializerMethodField()
    atp = serializers.SerializerMethodField()
    movement_tier = serializers.SerializerMethodField()

    class Meta:
        model = SKU
        fields = [
            "id", "sku_code", "barcode", "color", "unit_type", "brand_name", "item_type",
            "base_price", "dynamic_price", "rack", "physical_stock", "atp", "movement_tier",
        ]

    def _balance(self, obj):
        return getattr(obj, "balance", None)

    def get_physical_stock(self, obj):
        bal = self._balance(obj)
        return float(bal.physical_stock) if bal else 0

    def get_atp(self, obj):
        bal = self._balance(obj)
        return float(atp_for_balance(bal)) if bal else 0

    def get_movement_tier(self, obj):
        bal = self._balance(obj)
        if not bal:
            return "unknown"
        score = movement_score(obj, bal)
        return tier_for_score(score)


class AdminSKUSerializer(SKUSerializer):
    """Everything Billing/Warehouse/Packaging see, PLUS cost, margin, and pricing suggestions.
    Only ever instantiated for requests where request.user's role == ADMIN — see views.py."""
    margin_pct = serializers.SerializerMethodField()
    suggested_price = serializers.SerializerMethodField()
    movement_score_value = serializers.SerializerMethodField()

    class Meta(SKUSerializer.Meta):
        fields = SKUSerializer.Meta.fields + [
            "cost_price", "mrp", "margin_pct", "suggested_price", "movement_score_value",
        ]

    def get_margin_pct(self, obj):
        if obj.cost_price == 0:
            return None
        return round(float((obj.dynamic_price - obj.cost_price) / obj.cost_price) * 100, 1)

    def get_suggested_price(self, obj):
        bal = self._balance(obj)
        if not bal:
            return float(obj.dynamic_price)
        score = movement_score(obj, bal)
        t = tier_for_score(score)
        return float(suggested_price(obj, bal, t))

    def get_movement_score_value(self, obj):
        bal = self._balance(obj)
        return movement_score(obj, bal) if bal else 0


class InventoryLedgerSerializer(serializers.ModelSerializer):
    sku_code = serializers.CharField(source="sku.sku_code", read_only=True)
    actor_name = serializers.CharField(source="actor.full_name", read_only=True, default=None)

    class Meta:
        model = InventoryLedger
        fields = [
            "id", "sku", "sku_code", "event_type", "quantity_delta", "unit_cost",
            "reference", "actor", "actor_name", "reason", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class BillItemSerializer(serializers.ModelSerializer):
    sku_code = serializers.CharField(source="sku.sku_code", read_only=True)

    class Meta:
        model = BillItem
        fields = ["sku", "sku_code", "quantity", "unit_price"]


class BillCreateSerializer(serializers.ModelSerializer):
    """Input serializer for POS checkout. unit_cost_snapshot and totals are computed
    server-side inside the atomic transaction — never trusted from the client."""
    items = BillItemSerializer(many=True)

    class Meta:
        model = Bill
        fields = ["channel", "payment_mode", "items"]


class BillReadSerializer(serializers.ModelSerializer):
    items = BillItemSerializer(many=True, read_only=True)
    cashier_name = serializers.CharField(source="cashier.full_name", read_only=True)

    class Meta:
        model = Bill
        fields = [
            "id", "bill_number", "channel", "cashier_name", "payment_mode",
            "taxable_value", "gst_amount", "total_amount", "items", "created_at",
        ]


class StaffLoginSerializer(serializers.Serializer):
    staff_id = serializers.UUIDField()
    pin = serializers.CharField(max_length=6, min_length=4)


# ---------------------------------------------------------------------------
# Admin management: staff accounts, brands, and SKU CRUD.
# Every viewset that uses these is gated to Admin-only in views.py — never rely
# on the serializer alone to enforce access.
# ---------------------------------------------------------------------------
class StaffUserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = StaffUser
        fields = ["id", "full_name", "role", "username", "is_active"]


class StaffUserCreateSerializer(serializers.Serializer):
    """PIN is write-only input here — never echoed back in any response."""
    username = serializers.SlugField(max_length=150)
    full_name = serializers.CharField(max_length=120)
    role = serializers.ChoiceField(choices=Role.choices)
    pin = serializers.CharField(min_length=4, max_length=6, write_only=True)


class StaffUserUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=120, required=False)
    role = serializers.ChoiceField(choices=Role.choices, required=False)
    is_active = serializers.BooleanField(required=False)


class PinResetSerializer(serializers.Serializer):
    pin = serializers.CharField(min_length=4, max_length=6, write_only=True)


class BrandCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ["id", "item_type", "name", "fabric"]


class SKUManageSerializer(serializers.ModelSerializer):
    """Full read/write access to a SKU's own fields, for the Admin catalog manager.
    Does NOT touch physical_stock — that only ever changes via the ledger (StockMovementView)."""
    class Meta:
        model = SKU
        fields = [
            "id", "product", "sku_code", "barcode", "color", "unit_type",
            "cost_price", "mrp", "base_price", "dynamic_price",
            "safety_stock_threshold", "rack", "is_online_enabled", "is_active",
        ]
        read_only_fields = ["id"]


class SKUCreateSerializer(serializers.Serializer):
    """Convenience input for 'add a new colour under an existing brand' —
    finds-or-creates the Product row so admins never have to think about that layer."""
    brand_id = serializers.UUIDField()
    color = serializers.CharField(max_length=60)
    unit_type = serializers.ChoiceField(choices=SKU.UNIT_CHOICES)
    cost_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    mrp = serializers.DecimalField(max_digits=10, decimal_places=2)
    base_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    rack = serializers.CharField(max_length=20, required=False, default="")
    opening_stock = serializers.DecimalField(max_digits=12, decimal_places=3, default=0)
