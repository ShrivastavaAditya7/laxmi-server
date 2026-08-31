"""
Reusable simulation logic, shared by both:
  - the management command (simulate_activity.py) for local/CLI use
  - the DemoControlView API endpoint, for hosts with no shell/CLI access
    (e.g. Render's free tier) — triggered by buttons in the Admin UI instead.
"""
import random
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import (
    SKU, InventoryBalance, InventoryLedger, LedgerEventType,
    Bill, BillItem, Channel, PaymentMode, StaffUser,
)

PAYMENT_MODES = [PaymentMode.UPI, PaymentMode.UPI, PaymentMode.CASH, PaymentMode.CARD]


def daily_sales_for(index):
    if index % 23 == 22:
        return 0.02
    if index % 9 == 8:
        return 0.18
    good = [4.6, 3.1, 1.9, 1.2, 0.7]
    return good[index % len(good)]


def quantity_for(sku, rate):
    if sku.unit_type == "metre":
        return round(random.uniform(1.0, 4.0) * max(rate, 0.3), 2)
    return random.randint(1, 2)


def write_sale(sku, qty, cashier, when):
    balance, _ = InventoryBalance.objects.get_or_create(sku=sku)
    available = balance.physical_stock - balance.reserved_online - balance.damaged_qty
    if qty > available:
        qty = max(available, 0)
    if qty <= 0:
        return False

    price = sku.dynamic_price
    gst_rate = sku.product.gst_rate / Decimal("100")
    line_total = Decimal(str(qty)) * price
    line_taxable = line_total / (1 + gst_rate)
    line_gst = line_total - line_taxable

    bill = Bill.objects.create(
        id=uuid.uuid4(), bill_number=f"LT-DEMO-{uuid.uuid4().hex[:8]}",
        channel=Channel.IN_STORE, cashier=cashier, payment_mode=random.choice(PAYMENT_MODES),
        taxable_value=line_taxable.quantize(Decimal("0.01")),
        gst_amount=line_gst.quantize(Decimal("0.01")),
        total_amount=line_total.quantize(Decimal("0.01")),
    )
    Bill.objects.filter(pk=bill.pk).update(created_at=when)

    BillItem.objects.create(
        id=uuid.uuid4(), bill=bill, sku=sku, quantity=Decimal(str(qty)),
        unit_price=price, unit_cost_snapshot=sku.cost_price,
    )
    ledger = InventoryLedger.objects.create(
        id=uuid.uuid4(), sku=sku, event_type=LedgerEventType.SALE_COUNTER,
        quantity_delta=-Decimal(str(qty)), unit_cost=sku.cost_price,
        actor=cashier, reference=bill.bill_number,
    )
    InventoryLedger.objects.filter(pk=ledger.pk).update(created_at=when)

    balance.physical_stock = balance.physical_stock - Decimal(str(qty))
    balance.last_sale_at = when
    balance.save()
    return True


def run_backfill(days=14):
    skus = list(SKU.objects.filter(is_active=True).select_related("product__brand").order_by("sku_code"))
    cashiers = list(StaffUser.objects.filter(role="BILLING"))
    if not skus or not cashiers:
        return 0
    now = timezone.now()
    total = 0
    for day_offset in range(days, 0, -1):
        day = now - timedelta(days=day_offset)
        for i, sku in enumerate(skus):
            rate = daily_sales_for(i)
            if random.random() > min(rate, 1.0):
                continue
            qty = quantity_for(sku, rate)
            sale_time = day.replace(hour=random.randint(10, 20), minute=random.randint(0, 59), second=random.randint(0, 59))
            if write_sale(sku, qty, random.choice(cashiers), sale_time):
                total += 1
    return total


# ---------------------------------------------------------------------------
# In-process live ticker — a background thread inside the same web process.
# Works on hosts with no separate "worker" service (e.g. Render free tier),
# since it just runs inside whatever process is already serving the API.
# Only one instance runs per process; calling start() again while already
# running is a no-op, and stop() sets an Event the loop checks each cycle.
# ---------------------------------------------------------------------------
class LiveTicker:
    _thread = None
    _stop_event = threading.Event()

    @classmethod
    def is_running(cls):
        return cls._thread is not None and cls._thread.is_alive()

    @classmethod
    def start(cls, interval=5):
        if cls.is_running():
            return False
        cls._stop_event.clear()
        cls._thread = threading.Thread(target=cls._loop, args=(interval,), daemon=True)
        cls._thread.start()
        return True

    @classmethod
    def stop(cls):
        cls._stop_event.set()
        return True

    @classmethod
    def _loop(cls, interval):
        skus = list(SKU.objects.filter(is_active=True).select_related("product__brand").order_by("sku_code"))
        cashiers = list(StaffUser.objects.filter(role="BILLING"))
        if not skus or not cashiers:
            return
        weights = [daily_sales_for(i) + 0.05 for i in range(len(skus))]
        while not cls._stop_event.is_set():
            sku = random.choices(skus, weights=weights, k=1)[0]
            qty = quantity_for(sku, 1)
            write_sale(sku, qty, random.choice(cashiers), timezone.now())
            cls._stop_event.wait(random.uniform(interval * 0.5, interval * 1.5))
