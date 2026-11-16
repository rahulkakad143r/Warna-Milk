"""Billing business logic: bill numbering, totals and stock deduction."""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Bill, BillItem, Inventory, Payment, PaymentMethod

CENTS = Decimal("0.01")


def _q(value):
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def next_bill_number():
    """Format: GKB-YYYYMMDD-0001, incremented per day.

    Two cashiers can check out at the same instant, so the generated number is
    retried on a unique-constraint clash instead of returning a 500.
    """
    today = timezone.localdate()
    prefix = f"GKB-{today:%Y%m%d}-"
    for _ in range(10):
        last = (
            Bill.objects.filter(bill_number__startswith=prefix)
            .order_by("-bill_number")
            .values_list("bill_number", flat=True)
            .first()
        )
        seq = int(last[len(prefix):]) + 1 if last else 1
        candidate = f"{prefix}{seq:04d}"
        if not Bill.objects.filter(bill_number=candidate).exists():
            return candidate
    raise RuntimeError("Could not allocate a unique bill number.")


def _item_tax_rate(item):
    """The GST rate saved on the product, not one global bill rate."""
    return Decimal(getattr(item.product, "tax_rate", 0) or 0)


def _tax_breakdown(cart_items, discount=Decimal("0")):
    """Return subtotal, capped discount and one (item, rate, tax) row per line.

    A bill-level discount is shared across the lines in proportion to their
    value, so every product is taxed on what the customer actually paid for it.
    """
    items = list(cart_items)
    subtotal = _q(sum((i.line_total for i in items), Decimal("0")))
    discount = _q(max(Decimal("0"), Decimal(discount or 0)))
    if discount > subtotal:
        discount = subtotal
    taxable = subtotal - discount
    ratio = (taxable / subtotal) if subtotal else Decimal("0")

    rows = []
    for item in items:
        rate = _item_tax_rate(item)
        line_taxable = _q(item.line_total * ratio)
        rows.append((item, rate, _q(line_taxable * rate / Decimal("100"))))
    return subtotal, discount, rows


def compute_totals(cart_items, discount=Decimal("0"), tax_rate=None):
    """Compute cart totals.

    With no `tax_rate`, each line uses its own product GST rate (the normal
    path). Passing a rate explicitly keeps the old uniform-rate helper working
    for callers that need it, such as historical scripts.
    """
    items = list(cart_items)
    subtotal = _q(sum((i.line_total for i in items), Decimal("0")))
    discount = _q(max(Decimal("0"), Decimal(discount or 0)))
    if discount > subtotal:
        discount = subtotal
    taxable = subtotal - discount

    if tax_rate is not None:
        tax_amount = _q(taxable * (Decimal(tax_rate) / Decimal("100")))
    else:
        _, _, rows = _tax_breakdown(items, discount)
        tax_amount = _q(sum((row[2] for row in rows), Decimal("0")))
    return subtotal, discount, tax_amount, _q(taxable + tax_amount)


def compute_product_discount_totals(cart_items):
    """Compute totals using each product's discount before calculating its GST."""
    items = list(cart_items)
    subtotal = _q(sum((item.line_total for item in items), Decimal("0")))
    discount = _q(sum((item.line_discount for item in items), Decimal("0")))
    tax_amount = _q(sum(
        (
            _q((item.line_total - item.line_discount) * _item_tax_rate(item)
               / Decimal("100"))
            for item in items
        ),
        Decimal("0"),
    ))
    return subtotal, discount, tax_amount, _q(subtotal - discount + tax_amount)


@transaction.atomic
def checkout(cart, *, cashier=None, customer=None, discount=None,
             tax_rate=None, method=PaymentMethod.CASH, reference=""):
    """Turn a cart into a paid bill and deduct stock. Raises ValueError if stock is short."""
    items = list(cart.items)
    if not items:
        raise ValueError("Cart is empty.")

    for item in items:
        inv = Inventory.objects.select_for_update().filter(product=item.product).first()
        if inv is None:
            raise ValueError(f"No inventory record for {item.product.name}.")
        if inv.quantity < item.quantity:
            raise ValueError(
                f"Only {inv.quantity} left of {item.product.name} (requested {item.quantity})."
            )

    if discount is None:
        subtotal, discount, _tax, _total = compute_product_discount_totals(items)
        rows = [
            (
                item,
                _item_tax_rate(item),
                _q((item.line_total - item.line_discount)
                   * _item_tax_rate(item) / Decimal("100")),
            )
            for item in items
        ]
    else:
        subtotal, discount, rows = _tax_breakdown(items, discount)
    if tax_rate is not None:
        # Explicit override: one rate for the whole bill.
        taxable = subtotal - discount
        ratio = (taxable / subtotal) if subtotal else Decimal("0")
        rate = Decimal(tax_rate)
        rows = [
            (item, rate, _q(_q(item.line_total * ratio) * rate / Decimal("100")))
            for item in items
        ]
    tax_amount = _q(sum((row[2] for row in rows), Decimal("0")))
    total = _q(subtotal - discount + tax_amount)
    effective_rate = _q((tax_amount / (subtotal - discount)) * Decimal("100")) \
        if subtotal > discount else Decimal("0.00")

    bill = Bill.objects.create(
        bill_number=next_bill_number(),
        customer=customer,
        cashier=cashier,
        subtotal=subtotal,
        discount=discount,
        tax_rate=effective_rate,
        tax_amount=tax_amount,
        total=total,
        status="PAID",
    )

    bill_items = []
    for item, rate, line_tax in rows:
        line_total = _q(item.product.price * item.quantity)
        bill_items.append(
            BillItem(
                bill=bill,
                product=item.product,
                product_name=item.product.name,
                quantity=item.quantity,
                unit_price=item.product.price,
                line_total=line_total,
                tax_rate=rate,
                tax_amount=line_tax,
            )
        )
        Inventory.objects.filter(product=item.product).update(quantity=F("quantity") - item.quantity)
    BillItem.objects.bulk_create(bill_items)

    Payment.objects.create(
        bill=bill, method=method, amount=total, reference=reference or ""
    )

    cart.cart_items.all().delete()
    return bill


def restock(product, quantity):
    Inventory.objects.filter(product=product).update(quantity=F("quantity") + quantity)
