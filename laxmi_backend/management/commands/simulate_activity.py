"""
Populates realistic sales history so the dashboard looks like a real, active
store instead of a freshly-seeded empty one — for demos/presentations.

Two modes:
  python manage.py simulate_activity --days 14
      Backfills the past N days with sales matching each SKU's intended
      movement profile (fast/medium/slow/dead), written as real Bill,
      BillItem, and InventoryLedger rows with backdated timestamps.

  python manage.py simulate_activity --live
      Runs forever, creating one realistic sale every few seconds against
      the CURRENT timestamp. Leave this running in a spare terminal during
      a live presentation — refreshing the dashboard mid-demo will show
      numbers that have visibly moved.

Both modes reuse the same profile weighting as the original frontend mock
data, so a demo run "looks like" the same well-run store the prototype
described, just backed by real ledger rows this time.
"""
import random
import time
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from laxmi_backend.models import (
    SKU, InventoryBalance, InventoryLedger, LedgerEventType,
    Bill, BillItem, Channel, PaymentMode, StaffUser,
)

# Index-based profile, mirroring profileFor() from the frontend catalog builder.
def daily_sales_for(index):
    if index % 23 == 22:
        return 0.02       # dead stock — almost never sells
    if index % 9 == 8:
        return 0.18        # slow mover
    good = [4.6, 3.1, 1.9, 1.2, 0.7]
    return good[index % len(good)]


PAYMENT_MODES = [PaymentMode.UPI, PaymentMode.UPI, PaymentMode.CASH, PaymentMode.CARD]  # weighted toward UPI


class Command(BaseCommand):
    help = "Backfills or live-streams realistic sales activity for demos."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="How many past days to backfill.")
        parser.add_argument("--live", action="store_true", help="Run forever, creating live sales every few seconds.")
        parser.add_argument("--interval", type=int, default=5, help="Seconds between live sales (default 5).")

    def handle(self, *args, **options):
        skus = list(SKU.objects.filter(is_active=True).select_related("product__brand").order_by("sku_code"))
        if not skus:
            self.stdout.write(self.style.ERROR("No SKUs found — run seed_catalog first."))
            return
        cashiers = list(StaffUser.objects.filter(role="BILLING"))
        if not cashiers:
            self.stdout.write(self.style.ERROR("No billing staff found — run seed_catalog first."))
            return

        if options["live"]:
            self.run_live(skus, cashiers, options["interval"])
        else:
            self.run_backfill(skus, cashiers, options["days"])

    # -----------------------------------------------------------------
    def run_backfill(self, skus, cashiers, days):
        total_bills = 0
        now = timezone.now()
        for day_offset in range(days, 0, -1):
            day = now - timedelta(days=day_offset)
            for i, sku in enumerate(skus):
                rate = daily_sales_for(i)
                # Poisson-ish: most days sell 0, some days sell 1-2 units, fast movers sell most days.
                if random.random() > min(rate, 1.0):
                    continue
                qty = self._quantity_for(sku, rate)
                sale_time = day.replace(
                    hour=random.randint(10, 20), minute=random.randint(0, 59), second=random.randint(0, 59)
                )
                self._write_sale(sku, qty, random.choice(cashiers), sale_time)
                total_bills += 1
        self.stdout.write(self.style.SUCCESS(f"Backfilled {total_bills} sales across {days} days."))

    def run_live(self, skus, cashiers, interval):
        self.stdout.write(self.style.SUCCESS(
            f"Live demo mode — writing a sale every ~{interval}s. Press Ctrl+C to stop."
        ))
        weights = [daily_sales_for(i) + 0.05 for i in range(len(skus))]  # avoid zero-weight
        try:
            while True:
                sku = random.choices(skus, weights=weights, k=1)[0]
                qty = self._quantity_for(sku, 1)
                bal, _ = InventoryBalance.objects.get_or_create(sku=sku)
                if bal.physical_stock - bal.reserved_online - bal.damaged_qty >= qty:
                    self._write_sale(sku, qty, random.choice(cashiers), timezone.now())
                    self.stdout.write(f"  sold {qty} {sku.unit_type} of {sku.sku_code}")
                time.sleep(random.uniform(interval * 0.5, interval * 1.5))
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS("\nStopped live demo mode."))

    # -----------------------------------------------------------------
    def _quantity_for(self, sku, rate):
        if sku.unit_type == "metre":
            return round(random.uniform(1.0, 4.0) * max(rate, 0.3), 2)
        return random.randint(1, 2)

    def _write_sale(self, sku, qty, cashier, when):
        balance, _ = InventoryBalance.objects.get_or_create(sku=sku)
        available = balance.physical_stock - balance.reserved_online - balance.damaged_qty
        if qty > available:
            qty = max(available, 0)
        if qty <= 0:
            return

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
        Bill.objects.filter(pk=bill.pk).update(created_at=when)  # backdate; auto_now_add ignores direct assignment on create

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
