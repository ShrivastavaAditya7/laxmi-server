"""
Business logic for movement classification and dynamic pricing.
Kept as plain functions (no Django ORM writes here) so they can be unit-tested
in isolation and reused by a Celery task, a management command, or an API view.

This mirrors the scoring formula used in the frontend prototype so admin-facing
numbers never disagree between the two.
"""
import math
from datetime import datetime, timezone
from decimal import Decimal


def days_since(dt):
    if dt is None:
        return 999
    return max((datetime.now(timezone.utc) - dt).days, 0)


def daily_velocity(sku, window_days=30):
    """Average units sold per day over the trailing window, from the ledger —
    never trust a cached counter; derive it from SALE_* events."""
    from .models import InventoryLedger, LedgerEventType
    from django.utils import timezone as dj_tz
    from datetime import timedelta

    since = dj_tz.now() - timedelta(days=window_days)
    qs = InventoryLedger.objects.filter(
        sku=sku,
        event_type__in=[LedgerEventType.SALE_COUNTER, LedgerEventType.SALE_ONLINE],
        created_at__gte=since,
    )
    total_sold = -sum((row.quantity_delta for row in qs), Decimal("0"))  # sales are negative deltas
    return float(total_sold) / window_days if total_sold else 0.0


def atp_for_balance(balance):
    if balance is None:
        return Decimal("0")
    return max(balance.physical_stock - balance.reserved_online - balance.damaged_qty, Decimal("0"))


def movement_score(sku, balance, window_days=30):
    """0-100 weighted score: 40% velocity, 25% sell-through, 20% recency, 15% turnover."""
    v = daily_velocity(sku, window_days)
    physical = float(balance.physical_stock) if balance else 0.0
    days_in_stock = days_since(balance.last_purchase_at) if balance else 999

    velocity_norm = min(1.0, v / 4.0)
    sell_through = min(1.0, (v * 30) / (physical + v * 30)) if (physical + v * 30) > 0 else 0.0
    recency = max(0.0, 1 - days_in_stock / 200)
    turnover = min(1.0, (v * 30) / max(1.0, physical))

    score = 0.40 * velocity_norm + 0.25 * sell_through + 0.20 * recency + 0.15 * turnover
    return round(score * 100)


def tier_for_score(score):
    if score >= 70:
        return "fast"
    if score >= 45:
        return "medium"
    if score >= 20:
        return "slow"
    return "dead"


# ABC value tier — based on selling price, not sales volume, so a slow-moving
# wedding lehenga is never treated the same as a slow-moving dupatta.
def abc_tier(sku):
    sp = float(sku.dynamic_price)
    if sp >= 1500:
        return "A"
    if sp >= 600:
        return "B"
    return "C"


PROFIT_MATRIX = {
    "A": {"fast": "Protect price, restock", "medium": "Reorder on schedule",
          "slow": "Bundle / promote — capital at risk", "dead": "Owner alert: clear now"},
    "B": {"fast": "Reorder", "medium": "Hold",
          "slow": "Mild discount ~8%", "dead": "Discount 15–20%"},
    "C": {"fast": "Reorder in bulk", "medium": "Hold",
          "slow": "Auto-discount, low priority", "dead": "Bulk / wholesale clearance"},
}


def matrix_action(sku, tier):
    return PROFIT_MATRIX[abc_tier(sku)][tier]


def suggested_price(sku, balance, tier):
    """
    Fast:   min(MRP, price * (1 + surge))          surge bigger when ATP is low
    Medium: hold current dynamic_price
    Slow:   discount ~8%, floored at cost + 5% margin
    Dead:   discount ~25%, floored at cost (never sell below cost without owner override)
    """
    price = float(sku.dynamic_price)
    cp = float(sku.cost_price)
    mrp = float(sku.mrp)
    atp = float(atp_for_balance(balance))

    if tier == "fast":
        bump = 1.05 if atp < 8 else 1.02
        return round(min(mrp, price * bump))
    if tier == "medium":
        return round(price)
    if tier == "slow":
        return round(max(price * 0.92, cp * 1.05))
    # dead
    return round(max(price * 0.75, cp * 1.0))


def reorder_point(avg_daily_sales, lead_time_days, safety_stock):
    return avg_daily_sales * lead_time_days + safety_stock


def safety_stock_simple(avg_daily_sales, buffer_days=3):
    return avg_daily_sales * buffer_days


def gmroi(gross_margin, average_inventory_investment):
    if average_inventory_investment <= 0:
        return 0.0
    return round(gross_margin / average_inventory_investment, 2)
