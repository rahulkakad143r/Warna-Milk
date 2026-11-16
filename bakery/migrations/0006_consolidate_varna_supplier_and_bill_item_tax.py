from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

CENTS = Decimal("0.01")


def _q(value):
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def consolidate_supplier_and_tax(apps, schema_editor):
    Supplier = apps.get_model("bakery", "Supplier")
    Bill = apps.get_model("bakery", "Bill")

    default = Supplier.objects.filter(name__iexact="Varna Milk").first()
    legacy = Supplier.objects.filter(name__iexact="Varna Milk Plant").first()

    if default is None and legacy is not None:
        legacy.name = "Varna Milk"
        legacy.is_active = True
        legacy.save(update_fields=["name", "is_active"])
        default = legacy
    elif default is not None and legacy is not None and legacy.pk != default.pk:
        legacy.purchases.update(supplier=default)
        legacy.is_active = False
        legacy.save(update_fields=["is_active"])
    elif default is None:
        default = Supplier.objects.create(
            name="Varna Milk",
            contact_person="Varna Milk",
            phone="0000000000",
            is_active=True,
        )

    Supplier.objects.exclude(pk=default.pk).update(is_active=False)

    for bill in Bill.objects.prefetch_related("items"):
        items = list(bill.items.all())
        if not items:
            continue
        subtotal = sum((item.line_total for item in items), Decimal("0"))
        remaining = _q(bill.tax_amount)
        for index, item in enumerate(items):
            if index == len(items) - 1:
                item_tax = remaining
            elif subtotal:
                item_tax = _q(bill.tax_amount * item.line_total / subtotal)
                remaining -= item_tax
            else:
                item_tax = Decimal("0.00")
            item.tax_rate = bill.tax_rate
            item.tax_amount = item_tax
            item.save(update_fields=["tax_rate", "tax_amount"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("bakery", "0005_billitem_tax_amount_billitem_tax_rate"),
    ]

    operations = [
        migrations.RunPython(consolidate_supplier_and_tax, noop),
    ]
