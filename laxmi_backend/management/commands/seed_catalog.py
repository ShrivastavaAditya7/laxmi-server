"""
Seeds the database with:
  1. The same 54-SKU catalog used in the frontend (laxmi_textiles_app_v2.jsx)
  2. Opening stock via InventoryLedger (never writes InventoryBalance directly)
  3. A handful of StaffUser accounts you can actually log in as

Run with:  python manage.py seed_catalog
Safe to re-run: it skips anything that already exists instead of duplicating it.
"""
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from laxmi_backend.models import (
    ItemType, Brand, Product, SKU, InventoryLedger, InventoryBalance,
    LedgerEventType, StaffUser, Role,
)

CATALOG = [
    {"type": "Saree", "unit": "piece", "companies": [
        {"brand": "Nalli Silks", "fabric": "Kanjeevaram Silk", "colours": [
            ("Maroon", 4200, 12000, 9999), ("Royal Blue", 4400, 12500, 10499), ("Mustard Yellow", 4100, 11800, 9799)]},
        {"brand": "Pothys", "fabric": "Cotton Silk", "colours": [
            ("Peach", 850, 2200, 1899), ("Emerald Green", 880, 2300, 1949), ("Maroon", 820, 2150, 1849)]},
        {"brand": "Local Weaver", "fabric": "Cotton", "colours": [
            ("Rust", 420, 1100, 899), ("Teal", 400, 1050, 869), ("Beige", 380, 1000, 829)]},
    ]},
    {"type": "Suiting Fabric", "unit": "metre", "companies": [
        {"brand": "Raymond", "fabric": "Wool Blend", "colours": [
            ("Charcoal Grey", 950, 3200, 2799), ("Navy", 920, 3100, 2699), ("Black", 980, 3300, 2899)]},
        {"brand": "Siyaram's", "fabric": "Poly-Viscose", "colours": [
            ("Steel Blue", 380, 1100, 899), ("Navy", 360, 1050, 869), ("Brown", 370, 1080, 889)]},
        {"brand": "Vimal", "fabric": "Poly-Viscose", "colours": [
            ("Grey", 260, 750, 599), ("Black", 250, 720, 579), ("Navy", 255, 740, 589)]},
    ]},
    {"type": "Shirting Fabric", "unit": "metre", "companies": [
        {"brand": "Raymond", "fabric": "Cotton", "colours": [
            ("White", 220, 650, 549), ("Sky Blue", 215, 640, 539), ("Beige", 225, 660, 559)]},
        {"brand": "Siyaram's", "fabric": "Cotton Blend", "colours": [
            ("White", 180, 520, 449), ("Light Blue", 175, 510, 439), ("Pink Stripe", 185, 530, 459)]},
        {"brand": "Digjam", "fabric": "Premium Cotton", "colours": [
            ("White", 300, 850, 699), ("Checked Blue", 310, 870, 719), ("Cream", 295, 830, 689)]},
    ]},
    {"type": "Kurta", "unit": "piece", "companies": [
        {"brand": "Manyavar", "fabric": "Silk Blend", "colours": [
            ("Cream", 650, 1800, 1499), ("Maroon", 680, 1850, 1549), ("Navy", 660, 1820, 1519)]},
        {"brand": "Fabindia", "fabric": "Cotton", "colours": [
            ("Olive", 480, 1400, 1199), ("Mustard", 470, 1380, 1179), ("White", 460, 1350, 1149)]},
        {"brand": "Local Tailor", "fabric": "Cotton", "colours": [
            ("Sky Blue", 220, 650, 549), ("Grey", 215, 640, 539), ("White", 210, 630, 529)]},
    ]},
    {"type": "Bedsheet Set", "unit": "set", "companies": [
        {"brand": "Bombay Dyeing", "fabric": "Cotton", "colours": [
            ("Floral Print", 520, 1500, 1299), ("Solid White", 500, 1450, 1249), ("Geometric Print", 530, 1520, 1319)]},
        {"brand": "Spaces", "fabric": "Cotton Blend", "colours": [
            ("Floral Print", 380, 1100, 949), ("Striped", 370, 1080, 929), ("Solid Grey", 360, 1050, 899)]},
        {"brand": "Local Weaver", "fabric": "Cotton", "colours": [
            ("Floral Print", 260, 750, 649), ("Checked", 250, 730, 629), ("Plain Cream", 240, 700, 599)]},
    ]},
    {"type": "Dupatta", "unit": "piece", "companies": [
        {"brand": "Meena Bazaar", "fabric": "Chiffon", "colours": [
            ("Pink", 150, 450, 399), ("Turquoise", 145, 440, 389), ("Lavender", 148, 445, 394)]},
        {"brand": "Fabindia", "fabric": "Cotton Block Print", "colours": [
            ("Indigo Print", 180, 520, 449), ("Rust Print", 175, 510, 439), ("Green Print", 178, 515, 444)]},
        {"brand": "Local Weaver", "fabric": "Georgette", "colours": [
            ("Pink", 90, 280, 249), ("Yellow", 85, 270, 239), ("Peach", 88, 275, 244)]},
    ]},
]

STAFF = [
    ("suresh.gupta", "Suresh Gupta", Role.ADMIN, "1001"),
    ("ramesh.yadav", "Ramesh Yadav", Role.BILLING, "1002"),
    ("sita.devi", "Sita Devi", Role.BILLING, "1003"),
    ("anil.kumar", "Anil Kumar", Role.WAREHOUSE, "1004"),
    ("priya.singh", "Priya Singh", Role.PACKAGING, "1005"),
]


def short(s, n=3):
    return "".join(c for c in s if c.isalpha())[:n].upper()


class Command(BaseCommand):
    help = "Seeds the catalog (item types, brands, products, SKUs, opening stock) and staff accounts."

    @transaction.atomic
    def handle(self, *args, **options):
        i = 0
        created_skus = 0
        for group in CATALOG:
            item_type, _ = ItemType.objects.get_or_create(
                name=group["type"], defaults={"default_unit_type": group["unit"]}
            )
            for co in group["companies"]:
                brand, _ = Brand.objects.get_or_create(
                    item_type=item_type, name=co["brand"], defaults={"fabric": co["fabric"]}
                )
                product, _ = Product.objects.get_or_create(
                    brand=brand, name=f"{co['brand']} {group['type']}", defaults={"gst_rate": Decimal("5.00")}
                )
                for color, cp, mrp, sp in co["colours"]:
                    sku_code = f"{short(co['brand'])}-{short(co['fabric'])}-{short(color)}-B{1000+i}"
                    barcode = f"890{1000000 + i}"
                    if SKU.objects.filter(sku_code=sku_code).exists():
                        i += 1
                        continue
                    sku = SKU.objects.create(
                        id=uuid.uuid4(), product=product, sku_code=sku_code, barcode=barcode,
                        color=color, unit_type=group["unit"],
                        cost_price=Decimal(cp), mrp=Decimal(mrp), base_price=Decimal(sp), dynamic_price=Decimal(sp),
                        rack=f"{chr(65 + (i % 6))}-{1 + (i % 10)}",
                        parent_roll_id=f"ROLL-{1000+i}" if group["unit"] == "metre" else None,
                    )
                    base_stock = 70 if group["unit"] == "metre" else 32 if group["unit"] == "set" else 24
                    opening_qty = Decimal(base_stock)

                    InventoryLedger.objects.create(
                        id=uuid.uuid4(), sku=sku, event_type=LedgerEventType.OPENING,
                        quantity_delta=opening_qty, unit_cost=Decimal(cp), reason="Initial seed",
                    )
                    InventoryBalance.objects.create(sku=sku, physical_stock=opening_qty)
                    created_skus += 1
                    i += 1
        self.stdout.write(self.style.SUCCESS(f"Catalog: {created_skus} SKUs created."))

        created_staff = 0
        for username, full_name, role, pin in STAFF:
            if User.objects.filter(username=username).exists():
                continue
            user = User.objects.create_user(username=username, password=pin)
            StaffUser.objects.create(
                id=uuid.uuid4(), user=user, full_name=full_name, role=role, pin_hash="",
            )
            created_staff += 1
        self.stdout.write(self.style.SUCCESS(f"Staff: {created_staff} accounts created."))
        self.stdout.write(self.style.SUCCESS(
            "Login with e.g. username='suresh.gupta', pin='1001' via POST /api/auth/login/"
        ))
