"""Purchase (stock-in) business logic.

A purchase has no number of its own to allocate - it is identified by the
supplier's invoice number, which `Purchase.reference` keeps unique. What lives
here is the stock side-effect and the checks that must hold no matter which
screen posted the form.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F

from .models import Inventory, Product, Purchase, PurchaseItem, PurchaseStatus

CENTS = Decimal("0.01")


def _q(value):
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_purchase_totals(lines):
    """(subtotal, tax_amount, total) for a list of line dicts.

    Tax is worked out per line, because a purchase can mix products that
    carry different GST rates.
    """
    subtotal = _q(sum((_q(line["unit_cost"] * line["quantity"]) for line in lines), Decimal("0")))
    tax = _q(sum(
        (_q(_q(line["unit_cost"] * line["quantity"]) * line["tax_rate"] / Decimal("100"))
         for line in lines),
        Decimal("0"),
    ))
    return subtotal, tax, _q(subtotal + tax)


def validate_lines(lines, purchase_date):
    """Raise ValueError on the first problem with a message meant for the user."""
    if not lines:
        raise ValueError("Add at least one product line to the purchase.")

    seen = set()
    for line in lines:
        product = line.get("product")
        if product is None:
            raise ValueError("Every line needs a product.")
        if not product.is_active:
            raise ValueError("%s is inactive and cannot be purchased." % product.name)
        if product.pk in seen:
            raise ValueError(
                "%s appears on more than one line - combine the quantities." % product.name
            )
        seen.add(product.pk)

        quantity = line.get("quantity") or 0
        if quantity < 1:
            raise ValueError("Quantity for %s must be at least 1." % product.name)

        unit_cost = line.get("unit_cost") or Decimal("0")
        if unit_cost < 0:
            raise ValueError("Unit cost for %s cannot be negative." % product.name)

        expiry = line.get("expiry_date")
        if expiry and purchase_date and expiry < purchase_date:
            raise ValueError(
                "Expiry date for %s is before the purchase date." % product.name
            )


@transaction.atomic
def record_purchase(*, reference, supplier, purchase_date, lines, notes="",
                    created_by=None, status=PurchaseStatus.RECEIVED):
    """Save a purchase and, when it is RECEIVED, add the stock.

    `lines` is a list of dicts: product, quantity, unit_cost, tax_rate and an
    optional expiry_date. Raises ValueError if anything is wrong.
    """
    reference = (reference or "").strip()
    if not reference:
        raise ValueError("Supplier invoice number is required.")
    if supplier is None:
        raise ValueError("Select a supplier.")
    if Purchase.objects.filter(reference__iexact=reference).exists():
        raise ValueError("Invoice number %s has already been recorded." % reference)
    if purchase_date is None:
        raise ValueError("Purchase date is required.")

    lines = list(lines)
    validate_lines(lines, purchase_date)
    subtotal, tax_amount, total = compute_purchase_totals(lines)

    purchase = Purchase.objects.create(
        reference=reference,
        supplier=supplier,
        purchase_date=purchase_date,
        subtotal=subtotal,
        tax_amount=tax_amount,
        total=total,
        notes=notes or "",
        status=status,
        created_by=created_by,
    )

    items = []
    for line in lines:
        line_total = _q(line["unit_cost"] * line["quantity"])
        items.append(PurchaseItem(
            purchase=purchase,
            product=line["product"],
            quantity=line["quantity"],
            unit_cost=line["unit_cost"],
            tax_rate=line["tax_rate"],
            expiry_date=line.get("expiry_date"),
            line_total=line_total,
        ))
    PurchaseItem.objects.bulk_create(items)

    if status == PurchaseStatus.RECEIVED:
        apply_stock(purchase)
    return purchase


@transaction.atomic
def update_purchase(purchase, *, reference, supplier, purchase_date, lines,
                    notes=""):
    """Replace a purchase's details without changing its primary key.

    Received stock is reversed before the revised lines are applied, all
    within the same transaction. A cancelled purchase remains cancelled and
    therefore does not change inventory.
    """
    reference = (reference or "").strip()
    if not reference:
        raise ValueError("Supplier invoice number is required.")
    if supplier is None:
        raise ValueError("Select a supplier.")
    if Purchase.objects.filter(reference__iexact=reference).exclude(
        pk=purchase.pk
    ).exists():
        raise ValueError("Invoice number %s has already been recorded." % reference)
    if purchase_date is None:
        raise ValueError("Purchase date is required.")

    lines = list(lines)
    validate_lines(lines, purchase_date)
    subtotal, tax_amount, total = compute_purchase_totals(lines)
    was_received = purchase.status == PurchaseStatus.RECEIVED

    if was_received:
        reverse_stock(purchase)

    purchase.reference = reference
    purchase.supplier = supplier
    purchase.purchase_date = purchase_date
    purchase.subtotal = subtotal
    purchase.tax_amount = tax_amount
    purchase.total = total
    purchase.notes = notes or ""
    purchase.save(update_fields=[
        "reference", "supplier", "purchase_date", "subtotal",
        "tax_amount", "total", "notes",
    ])

    existing_items = {item.pk: item for item in purchase.items.all()}
    retained_ids = set()
    items_to_create = []
    for line in lines:
        item = line.get("item")
        if item is not None and item.pk in existing_items:
            item = existing_items[item.pk]
            item.product = line["product"]
            item.quantity = line["quantity"]
            item.unit_cost = line["unit_cost"]
            item.tax_rate = line["tax_rate"]
            item.expiry_date = line.get("expiry_date")
            item.line_total = _q(line["unit_cost"] * line["quantity"])
            item.save(update_fields=[
                "product", "quantity", "unit_cost", "tax_rate",
                "expiry_date", "line_total",
            ])
            retained_ids.add(item.pk)
        else:
            items_to_create.append(PurchaseItem(
                purchase=purchase,
                product=line["product"],
                quantity=line["quantity"],
                unit_cost=line["unit_cost"],
                tax_rate=line["tax_rate"],
                expiry_date=line.get("expiry_date"),
                line_total=_q(line["unit_cost"] * line["quantity"]),
            ))

    purchase.items.exclude(pk__in=retained_ids).delete()
    PurchaseItem.objects.bulk_create(items_to_create)

    if was_received:
        apply_stock(purchase)
    return purchase


def apply_stock(purchase):
    """Add every line of a RECEIVED purchase to stock.

    Batch expiry is tracked on the product, so the earliest known date wins -
    that is the batch that has to be sold first.
    """
    for item in purchase.items.select_related("product"):
        Inventory.objects.filter(product=item.product).update(
            quantity=F("quantity") + item.quantity
        )
        _merge_expiry(item.product, item.expiry_date)
    return purchase


def reverse_stock(purchase):
    """Take the stock back out, used when a purchase is removed."""
    for item in purchase.items.select_related("product"):
        Inventory.objects.filter(product=item.product).update(
            quantity=F("quantity") - item.quantity
        )
    return purchase


def _merge_expiry(product, expiry_date):
    """Keep the earliest date we know about, ignoring a blank one."""
    if not expiry_date:
        return
    current = product.expiry_date
    if current is None or expiry_date < current:
        Product.objects.filter(pk=product.pk).update(expiry_date=expiry_date)
        product.expiry_date = expiry_date
