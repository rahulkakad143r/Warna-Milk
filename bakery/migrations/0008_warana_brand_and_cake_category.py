from django.db import migrations

CAKE_DESCRIPTION = "Freshly baked cakes for every celebration."
DAIRY_DESCRIPTION = "Milk, curd, paneer and other dairy essentials."


def _merge_supplier(Supplier, old_name, new_name):
    old = Supplier.objects.filter(name__iexact=old_name).first()
    if old is None:
        return
    new = Supplier.objects.filter(name__iexact=new_name).exclude(pk=old.pk).first()
    if new is not None:
        old.purchases.update(supplier=new)
        old.delete()
        return
    old.name = new_name
    old.contact_person = new_name
    if (old.address or "").strip() in ("", "Varna, Maharashtra"):
        old.address = "Warana, Maharashtra"
    old.is_active = True
    old.save(update_fields=["name", "contact_person", "address", "is_active"])


def _rename_category(Category, old_name, new_name, description):
    old = Category.objects.filter(name__iexact=old_name).first()
    new = Category.objects.filter(name__iexact=new_name).first()
    if old is not None and new is not None and old.pk != new.pk:
        old.products.update(category=new)
        old.delete()
        target = new
    elif old is not None:
        old.name = new_name
        old.save(update_fields=["name"])
        target = old
    else:
        target = new
    if target is not None and (target.description or "").strip() in (
        "", "Celebration and slice cakes"
    ):
        target.description = description
        target.save(update_fields=["description"])


def rename_brand(apps, schema_editor):
    Supplier = apps.get_model("bakery", "Supplier")
    Category = apps.get_model("bakery", "Category")

    _merge_supplier(Supplier, "Varna Milk", "Warana Milk")
    _merge_supplier(Supplier, "Varna Milk Plant", "Warana Milk")
    _rename_category(Category, "Cakes", "Cake", CAKE_DESCRIPTION)

    dairy = Category.objects.filter(name__iexact="Dairy").first()
    if dairy is not None and not (dairy.description or "").strip():
        dairy.description = DAIRY_DESCRIPTION
        dairy.save(update_fields=["description"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("bakery", "0007_product_discount_percent"),
    ]

    operations = [
        migrations.RunPython(rename_brand, noop),
    ]
