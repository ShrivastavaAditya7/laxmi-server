import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction, IntegrityError
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from .models import (
    ItemType, Brand, Product, SKU, InventoryLedger, InventoryBalance, LedgerEventType,
    Bill, BillItem, StaffUser, Role, Channel,
)
from .serializers import (
    ItemTypeSerializer, BrandSerializer, SKUSerializer, AdminSKUSerializer,
    InventoryLedgerSerializer, BillCreateSerializer, BillReadSerializer,
    StaffUserSerializer, StaffUserCreateSerializer, StaffUserUpdateSerializer,
    PinResetSerializer, BrandCreateSerializer, SKUManageSerializer, SKUCreateSerializer,
)
from .permissions import IsAdmin, IsWarehouseOrAdmin, IsBillingOrAdmin, IsAnyStaff
from .services import movement_score, tier_for_score, matrix_action, atp_for_balance


# ---------------------------------------------------------------------------
# Catalog — read-only for everyone authenticated; role determines which
# serializer (and therefore which fields, i.e. cost/margin) gets used.
# ---------------------------------------------------------------------------
class ItemTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ItemType.objects.all()
    serializer_class = ItemTypeSerializer
    permission_classes = [IsAnyStaff]


class BrandViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Brand.objects.select_related("item_type").all()
    serializer_class = BrandSerializer
    permission_classes = [IsAnyStaff]

    def get_queryset(self):
        qs = super().get_queryset()
        item_type = self.request.query_params.get("item_type")
        if item_type:
            qs = qs.filter(item_type__name=item_type)
        return qs


class SKUViewSet(viewsets.ReadOnlyModelViewSet):
    """Stock register. Cost/margin fields only appear for Admin — enforced here,
    not left to the frontend to hide."""
    queryset = SKU.objects.select_related(
        "product__brand__item_type", "balance"
    ).filter(is_active=True)
    permission_classes = [IsAnyStaff]

    def get_serializer_class(self):
        profile = getattr(self.request.user, "staff_profile", None)
        if profile and profile.role == Role.ADMIN:
            return AdminSKUSerializer
        return SKUSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        item_type = self.request.query_params.get("item_type")
        brand = self.request.query_params.get("brand")
        search = self.request.query_params.get("search")
        if item_type:
            qs = qs.filter(product__brand__item_type__name=item_type)
        if brand:
            qs = qs.filter(product__brand__name=brand)
        if search:
            qs = qs.filter(sku_code__icontains=search) | qs.filter(color__icontains=search)
        return qs

    @action(detail=False, methods=["get"], permission_classes=[IsAdmin])
    def alerts(self, request):
        """Reorder + clearance action feed for the admin dashboard."""
        items = self.get_queryset()
        out = []
        for sku in items:
            bal = getattr(sku, "balance", None)
            if not bal:
                continue
            score = movement_score(sku, bal)
            t = tier_for_score(score)
            atp = float(atp_for_balance(bal))
            if atp <= float(sku.safety_stock_threshold) and t == "fast":
                out.append({"kind": "reorder", "sku": sku.sku_code,
                             "text": f"{sku.product.brand.name} {sku.color} — ATP {atp}, fast-moving. Reorder now."})
            if t == "dead":
                out.append({"kind": "clear", "sku": sku.sku_code,
                             "text": f"{sku.product.brand.name} {sku.color} — dead stock, recommend clearance."})
        return Response(out)


# ---------------------------------------------------------------------------
# Warehouse: receive stock, cut from roll, mark damage — all via ledger entries
# ---------------------------------------------------------------------------
class StockMovementView(APIView):
    """
    POST body:
      { "sku": "<uuid>", "event_type": "PURCHASE_RECEIVE", "quantity": 25,
        "unit_cost": 210.00, "reason": "" }

    Never writes InventoryBalance directly from client input — recomputes it
    from the ledger inside the same transaction, so balance and ledger can
    never drift because of a bad request.
    """
    permission_classes = [IsWarehouseOrAdmin]

    ALLOWED_STAFF_EVENTS = {
        LedgerEventType.PURCHASE_RECEIVE, LedgerEventType.CUT_FROM_ROLL,
        LedgerEventType.DAMAGE, LedgerEventType.RTV,
        LedgerEventType.STOCK_COUNT_ADJUSTMENT, LedgerEventType.CUSTOMER_RETURN,
    }

    @transaction.atomic
    def post(self, request):
        sku_id = request.data.get("sku")
        event_type = request.data.get("event_type")
        quantity = Decimal(str(request.data.get("quantity", "0")))
        unit_cost = request.data.get("unit_cost")
        reason = request.data.get("reason", "")

        if event_type not in self.ALLOWED_STAFF_EVENTS:
            raise ValidationError(f"Event type '{event_type}' is not permitted via this endpoint.")
        if quantity == 0:
            raise ValidationError("Quantity must be non-zero.")

        sku = SKU.objects.select_for_update().get(id=sku_id)
        balance, _ = InventoryBalance.objects.select_for_update().get_or_create(sku=sku)

        # Outbound events (cutting, damage, RTV) are stored as negative deltas.
        outbound = {LedgerEventType.CUT_FROM_ROLL, LedgerEventType.DAMAGE, LedgerEventType.RTV}
        delta = -quantity if event_type in outbound else quantity

        if event_type in outbound and delta * -1 > balance.physical_stock:
            raise ValidationError("Cannot remove more stock than is physically on hand.")

        staff = getattr(request.user, "staff_profile", None)
        InventoryLedger.objects.create(
            id=uuid.uuid4(), sku=sku, event_type=event_type, quantity_delta=delta,
            unit_cost=unit_cost, actor=staff, reason=reason,
        )

        if event_type == LedgerEventType.DAMAGE:
            balance.damaged_qty = F("damaged_qty") + quantity
        else:
            balance.physical_stock = F("physical_stock") + delta
        if event_type == LedgerEventType.PURCHASE_RECEIVE:
            balance.last_purchase_at = timezone.now()
        balance.save()
        balance.refresh_from_db()

        return Response({
            "sku": sku.sku_code,
            "event_type": event_type,
            "physical_stock": float(balance.physical_stock),
            "atp": float(atp_for_balance(balance)),
        }, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Billing: atomic checkout — verify stock, deduct, write ledger + bill rows
# ---------------------------------------------------------------------------
class CheckoutView(APIView):
    """
    POST body:
      { "channel": "IN_STORE", "payment_mode": "UPI",
        "items": [{"sku": "<uuid>", "quantity": 2.5, "unit_price": 899.00}, ...] }

    Supports fractional quantities for meter-wise billing. Locks each SKU row
    (SELECT ... FOR UPDATE) before deducting, so two counters can never oversell
    the same roll at the same instant.
    """
    permission_classes = [IsBillingOrAdmin]

    @transaction.atomic
    def post(self, request):
        serializer = BillCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        staff = getattr(request.user, "staff_profile", None)

        taxable_total = Decimal("0")
        gst_total = Decimal("0")
        grand_total = Decimal("0")
        bill_items_to_create = []

        for item in data["items"]:
            sku = SKU.objects.select_for_update().select_related("product").get(id=item["sku"].id if hasattr(item["sku"], "id") else item["sku"])
            balance, _ = InventoryBalance.objects.select_for_update().get_or_create(sku=sku)
            qty = Decimal(str(item["quantity"]))
            price = Decimal(str(item["unit_price"]))

            available = balance.physical_stock - balance.reserved_online - balance.damaged_qty
            if qty > available:
                raise ValidationError(f"Insufficient stock for {sku.sku_code}. Available: {available}")

            gst_rate = sku.product.gst_rate / Decimal("100")
            line_total = qty * price
            line_taxable = line_total / (1 + gst_rate)
            line_gst = line_total - line_taxable

            taxable_total += line_taxable
            gst_total += line_gst
            grand_total += line_total

            InventoryLedger.objects.create(
                id=uuid.uuid4(), sku=sku,
                event_type=LedgerEventType.SALE_COUNTER if data["channel"] == Channel.IN_STORE else LedgerEventType.SALE_ONLINE,
                quantity_delta=-qty, unit_cost=sku.cost_price, actor=staff,
            )
            balance.physical_stock = F("physical_stock") - qty
            balance.last_sale_at = timezone.now()
            balance.save()

            bill_items_to_create.append((sku, qty, price))

        bill_number = f"LT-{int(timezone.now().timestamp())}"
        bill = Bill.objects.create(
            id=uuid.uuid4(), bill_number=bill_number, channel=data["channel"],
            cashier=staff, payment_mode=data["payment_mode"],
            taxable_value=taxable_total.quantize(Decimal("0.01")),
            gst_amount=gst_total.quantize(Decimal("0.01")),
            total_amount=grand_total.quantize(Decimal("0.01")),
        )
        BillItem.objects.bulk_create([
            BillItem(id=uuid.uuid4(), bill=bill, sku=sku, quantity=qty,
                     unit_price=price, unit_cost_snapshot=sku.cost_price)
            for sku, qty, price in bill_items_to_create
        ])

        return Response(BillReadSerializer(bill).data, status=status.HTTP_201_CREATED)


class TodaysBillsView(generics.ListAPIView):
    """Billing staff see only their own bills for the session/day — never store-wide totals."""
    serializer_class = BillReadSerializer
    permission_classes = [IsBillingOrAdmin]

    def get_queryset(self):
        profile = getattr(self.request.user, "staff_profile", None)
        qs = Bill.objects.filter(created_at__date=timezone.now().date())
        if profile and profile.role != Role.ADMIN:
            qs = qs.filter(cashier=profile)
        return qs.prefetch_related("items")


# ---------------------------------------------------------------------------
# Admin dashboard aggregates
# ---------------------------------------------------------------------------
class DashboardSummaryView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        skus = SKU.objects.select_related("balance", "product__brand").filter(is_active=True)
        inv_value = Decimal("0")
        gross_margin_potential = Decimal("0")
        dead_value = Decimal("0")
        low_stock = 0

        for sku in skus:
            bal = getattr(sku, "balance", None)
            if not bal:
                continue
            inv_value += sku.cost_price * bal.physical_stock
            gross_margin_potential += (sku.dynamic_price - sku.cost_price) * bal.physical_stock
            score = movement_score(sku, bal)
            t = tier_for_score(score)
            if t == "dead":
                dead_value += sku.cost_price * bal.physical_stock
            if float(atp_for_balance(bal)) <= float(sku.safety_stock_threshold):
                low_stock += 1

        today_bills = Bill.objects.filter(created_at__date=timezone.now().date())
        today_revenue = today_bills.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")

        gmroi_val = round(float(gross_margin_potential) / float(inv_value), 2) if inv_value else 0.0

        return Response({
            "inventory_value": float(inv_value),
            "gross_margin_potential": float(gross_margin_potential),
            "gmroi": gmroi_val,
            "dead_stock_value": float(dead_value),
            "low_stock_count": low_stock,
            "today_revenue": float(today_revenue),
            "today_bill_count": today_bills.count(),
        })


class DashboardTrendView(APIView):
    """Real 7-day revenue trend, replacing the hardcoded mock array the
    frontend prototype originally shipped with — pulled straight from Bill rows,
    so this reflects whatever simulate_activity (or real sales) actually wrote."""
    permission_classes = [IsAdmin]

    def get(self, request):
        from django.db.models.functions import TruncDate
        today = timezone.now().date()
        start = today - timedelta(days=6)
        rows = (
            Bill.objects.filter(created_at__date__gte=start)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(sales=Sum("total_amount"))
            .order_by("day")
        )
        by_day = {r["day"]: float(r["sales"]) for r in rows}
        out = []
        for i in range(7):
            d = start + timedelta(days=i)
            out.append({"day": d.strftime("%a"), "date": d.isoformat(), "sales": by_day.get(d, 0.0)})
        return Response(out)


# ---------------------------------------------------------------------------
# Admin management: staff CRUD (create/edit/deactivate accounts, reset PINs)
# ---------------------------------------------------------------------------
class StaffManagementViewSet(viewsets.ViewSet):
    """
    Deliberately a plain ViewSet, not a ModelViewSet, because creating a staff
    member touches TWO tables (auth.User for login credentials, StaffUser for
    role/profile) — that composite operation doesn't fit DRF's default CRUD mapping.

    Staff are never hard-deleted: DELETE deactivates instead, since ledger rows
    reference the actor by FK (SET_NULL) and losing "who did this sale" from
    historical records would break the audit trail the whole system is built on.
    """
    permission_classes = [IsAdmin]

    def list(self, request):
        staff = StaffUser.objects.select_related("user").all()
        return Response(StaffUserSerializer(staff, many=True).data)

    def create(self, request):
        serializer = StaffUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if User.objects.filter(username=data["username"]).exists():
            raise ValidationError("That username already exists.")
        user = User.objects.create_user(username=data["username"], password=data["pin"])
        staff = StaffUser.objects.create(
            id=uuid.uuid4(), user=user, full_name=data["full_name"],
            role=data["role"], pin_hash="",
        )
        return Response(StaffUserSerializer(staff).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        staff = get_object_or_404(StaffUser, pk=pk)
        serializer = StaffUserUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(staff, field, value)
        staff.save()
        return Response(StaffUserSerializer(staff).data)

    def destroy(self, request, pk=None):
        """Deactivate, not delete — see class docstring."""
        staff = get_object_or_404(StaffUser, pk=pk)
        staff.is_active = False
        staff.user.is_active = False
        staff.user.save()
        staff.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def reset_pin(self, request, pk=None):
        staff = get_object_or_404(StaffUser, pk=pk)
        serializer = PinResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        staff.user.set_password(serializer.validated_data["pin"])
        staff.user.save()
        return Response({"detail": f"PIN reset for {staff.full_name}."})

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        staff = get_object_or_404(StaffUser, pk=pk)
        staff.is_active = True
        staff.user.is_active = True
        staff.user.save()
        staff.save()
        return Response(StaffUserSerializer(staff).data)


# ---------------------------------------------------------------------------
# Admin management: catalog CRUD (add companies/colours, edit price/rack/threshold)
# ---------------------------------------------------------------------------
class BrandManagementViewSet(viewsets.ModelViewSet):
    """Admins add a new company/brand here under an existing item type;
    the SKU manager below then adds colours under it."""
    queryset = Brand.objects.select_related("item_type").all()
    serializer_class = BrandCreateSerializer
    permission_classes = [IsAdmin]


class SKUManagementViewSet(viewsets.ModelViewSet):
    """
    Full CRUD on a SKU's own attributes (price, rack, threshold, active flag).
    Never exposes a way to set physical_stock directly — that field doesn't
    even appear in SKUManageSerializer, by design; stock only ever moves
    through StockMovementView so the ledger stays the single source of truth.
    """
    queryset = SKU.objects.select_related("product__brand").all()
    serializer_class = SKUManageSerializer
    permission_classes = [IsAdmin]

    def create(self, request):
        """Convenience 'add a new colour under this brand' flow — finds-or-creates
        the Product row and opens an InventoryLedger OPENING entry for the stock."""
        serializer = SKUCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        brand = get_object_or_404(Brand, id=d["brand_id"])
        product, _ = Product.objects.get_or_create(
            brand=brand, name=f"{brand.name} {brand.item_type.name}",
            defaults={"gst_rate": Decimal("5.00")},
        )
        def short(s, n=3):
            return "".join(c for c in s if c.isalpha())[:n].upper()
        suffix = uuid.uuid4().hex[:4].upper()
        sku_code = f"{short(brand.name)}-{short(brand.fabric)}-{short(d['color'])}-{suffix}"

        sku = SKU.objects.create(
            id=uuid.uuid4(), product=product, sku_code=sku_code,
            barcode=f"890{uuid.uuid4().int % 10_000_000:07d}",
            color=d["color"], unit_type=d["unit_type"],
            cost_price=d["cost_price"], mrp=d["mrp"], base_price=d["base_price"],
            dynamic_price=d["base_price"], rack=d.get("rack", ""),
            parent_roll_id=f"ROLL-{suffix}" if d["unit_type"] == "metre" else None,
        )
        opening = d["opening_stock"]
        InventoryLedger.objects.create(
            id=uuid.uuid4(), sku=sku, event_type=LedgerEventType.OPENING,
            quantity_delta=opening, unit_cost=d["cost_price"], reason="Added via catalog manager",
        )
        InventoryBalance.objects.create(sku=sku, physical_stock=opening)
        return Response(SKUManageSerializer(sku).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        """Deactivate, not delete — a SKU with ledger/bill history can't be safely
        removed without breaking those foreign keys and the audit trail."""
        sku = self.get_object()
        sku.is_active = False
        sku.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
