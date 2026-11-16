"""Seed the database with a cashier login, categories, products, suppliers,
stock and one example purchase."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from bakery import purchasing
from bakery.models import (LOW_STOCK_THRESHOLD, Category, Customer, Inventory,
                           Product, Supplier)

# name, description, GST rate (%)
CATEGORIES = [
    ("Bread", "Loaves and buns baked every morning", Decimal("5.00")),
    ("Cake", "Freshly baked cakes for every celebration.", Decimal("18.00")),
    ("Pastries", "Fresh from the oven, sold by the piece", Decimal("18.00")),
    ("Cookies", "Boxes, packs and biscuits", Decimal("18.00")),
    ("Dairy", "Milk, curd, paneer and cold items", Decimal("5.00")),
    ("Beverages", "Tea, coffee and cold drinks", Decimal("12.00")),
]

# name, category, price, unit, quantity, reorder, shelf life in days (None = no expiry)
PRODUCTS = [
    ("Milk Bread", "Bread", "60.00", "loaf", 24, 6, 3),
    ("Whole Wheat Bread", "Bread", "75.00", "loaf", 15, 5, 4),
    ("Butter Croissant", "Pastries", "45.00", "pcs", 40, 10, 1),
    ("Veg Puff", "Pastries", "35.00", "pcs", 35, 10, 1),
    ("Egg Curry Puff", "Pastries", "40.00", "pcs", 28, 8, 1),
    ("Chocolate Brownie", "Cake", "85.00", "pcs", 18, 6, 5),
    ("Vanilla Cup Cake", "Cake", "55.00", "pcs", 30, 8, 4),
    ("Fruit Cake 500g", "Cake", "320.00", "pcs", 8, 4, 7),
    ("Butter Cookie Box", "Cookies", "150.00", "box", 20, 5, 30),
    ("Oats Biscuit Pack", "Cookies", "70.00", "pack", 26, 6, 60),
    ("Toned Milk 500ml", "Dairy", "28.00", "pcs", 60, 15, 7),
    ("Curd 400g", "Dairy", "38.00", "pcs", 22, 6, 5),
    ("Paneer 200g", "Dairy", "95.00", "pcs", 14, 5, 4),
    ("Fresh Lime Juice", "Beverages", "40.00", "glass", 25, 8, 1),
    ("Masala Chai", "Beverages", "20.00", "cup", 50, 12, 1),
    # --- second batch: more items on the point-of-sale shelf ---
    ("Garlic Bread", "Bread", "55.00", "loaf", 18, 5, 3),
    ("Multigrain Bread", "Bread", "85.00", "loaf", 12, 4, 4),
    ("Dinner Rolls (6)", "Bread", "40.00", "pack", 30, 8, 2),
    ("Black Forest Cake 500g", "Cake", "350.00", "pcs", 6, 3, 3),
    ("Mango Cake Slice", "Cake", "60.00", "slice", 24, 6, 2),
    ("Red Velvet Cup Cake", "Cake", "70.00", "pcs", 20, 6, 4),
    ("Chocolate Pastry", "Pastries", "50.00", "pcs", 32, 8, 2),
    ("Pineapple Pastry", "Pastries", "50.00", "pcs", 30, 8, 2),
    ("Chocolate Chip Cookies", "Cookies", "120.00", "pack", 22, 6, 30),
    ("Coconut Crunch Cookies", "Cookies", "110.00", "pack", 18, 5, 45),
    ("Ghee 500ml", "Dairy", "280.00", "bottle", 12, 4, 90),
    ("Butter 100g", "Dairy", "58.00", "pack", 40, 10, 60),
    ("Cream 200ml", "Dairy", "85.00", "pack", 20, 6, 7),
    ("Lassi 300ml", "Dairy", "35.00", "bottle", 30, 8, 5),
    ("Flavoured Milk 200ml", "Dairy", "32.00", "bottle", 35, 10, 6),
    ("Cold Coffee 250ml", "Beverages", "55.00", "bottle", 28, 8, 2),
    ("Ice Tea 300ml", "Beverages", "30.00", "bottle", 32, 8, 7),
    ("Fruit Punch 250ml", "Beverages", "45.00", "bottle", 26, 6, 3),
]

SUPPLIERS = [
    ("Warana Milk", "Anil Kulkarni", "9822001100", "27AAACV1234A1Z5"),
]

# supplier, invoice number, (product name, quantity, unit cost)
PURCHASES = [
    ("Warana Milk", "WMP-2026-0188", [
        ("Toned Milk 500ml", 50, Decimal("23.00")),
        ("Curd 400g", 20, Decimal("32.00")),
        ("Paneer 200g", 10, Decimal("80.00")),
    ]),
    ("Warana Milk", "WMP-2026-0441", [
        ("Milk Bread", 20, Decimal("48.00")),
        ("Whole Wheat Bread", 10, Decimal("60.00")),
    ]),
]

SHELF_LIFE = {name: days for name, _, _, _, _, _, days in PRODUCTS}


class Command(BaseCommand):
    help = "Create demo users, categories, products, suppliers and stock."

    def handle(self, *args, **options):
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@waranamilk.local", "admin123")
            self.stdout.write("Created superuser admin / admin123")

        if not User.objects.filter(username="cashier").exists():
            User.objects.create_user("cashier", "cashier@waranamilk.local", "cashier123", is_staff=True)
            self.stdout.write("Created user cashier / cashier123")

        today = timezone.localdate()

        cats = {}
        for name, description, tax in CATEGORIES:
            cat, _ = Category.objects.get_or_create(
                name=name,
                defaults={"description": description, "tax_rate": tax},
            )
            cats[name] = cat

        products = {}
        for name, cat, price, unit, qty, _reorder, shelf in PRODUCTS:
            product, created = Product.objects.get_or_create(
                name=name,
                defaults={
                    "category": cats[cat],
                    "price": Decimal(price),
                    "unit": unit,
                    "tax_rate": cats[cat].tax_rate,  # tax follows the category
                    "description": f"Freshly prepared {name.lower()} from Warana Milk bakery.",
                },
            )
            products[name] = product
            inv, _ = Inventory.objects.get_or_create(product=product)
            if created:
                inv.quantity = qty
                inv.reorder_level = LOW_STOCK_THRESHOLD
                inv.save()
                if shelf:
                    product.expiry_date = today + timedelta(days=shelf)
                    product.save(update_fields=["expiry_date"])
            else:
                self.stdout.write(f"Skipped existing product: {name}")

        for name, phone in [("Ravi Kumar", "9800000001"), ("Anita Sharma", "9800000002")]:
            Customer.objects.get_or_create(phone=phone, defaults={"name": name})

        suppliers = {}
        for name, contact, phone, gst in SUPPLIERS:
            supplier, _ = Supplier.objects.get_or_create(
                name=name,
                defaults={
                    "contact_person": contact,
                    "phone": phone,
                    "gst_number": gst,
                    "address": "Warana, Maharashtra",
                },
            )
            suppliers[name] = supplier

        for supplier_name, reference, lines in PURCHASES:
            if self._already_seeded(reference):
                continue
            purchasing.record_purchase(
                reference=reference,
                supplier=suppliers[supplier_name],
                purchase_date=today,
                created_by=User.objects.filter(username="admin").first(),
                lines=[
                    {
                        "product": products[name],
                        "quantity": qty,
                        "unit_cost": cost,
                        "tax_rate": products[name].tax_rate,
                        "expiry_date": self._expiry(today, SHELF_LIFE.get(name)),
                    }
                    for name, qty, cost in lines
                ],
            )
            self.stdout.write(f"Recorded purchase {reference} from {supplier_name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed complete: {Category.objects.count()} categories, "
                f"{Product.objects.count()} products, {Inventory.objects.count()} stock records, "
                f"{Supplier.objects.count()} suppliers."
            )
        )

    @staticmethod
    def _already_seeded(reference):
        from bakery.models import Purchase
        return Purchase.objects.filter(reference=reference).exists()

    @staticmethod
    def _expiry(today, shelf_days):
        return today + timedelta(days=shelf_days) if shelf_days else None
