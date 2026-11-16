from django.contrib import admin
from django.utils.html import format_html

from .models import (Bill, BillItem, Cart, CartItem, Category, Customer,
                     Inventory, Payment, Product, Purchase, PurchaseItem,
                     Supplier)

admin.site.site_header = "Warana Milk Administration"
admin.site.site_title = "Warana Milk Admin"
admin.site.index_title = "Warana Milk data management"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "tax_rate", "product_count")
    search_fields = ("name",)

    @admin.display(description="Products")
    def product_count(self, obj):
        return obj.products.count()


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "unit", "tax_rate", "stock",
                    "expiry_date", "has_photo", "is_active")
    list_filter = ("category", "unit", "is_active")
    search_fields = ("name", "description")
    list_editable = ("price", "unit", "tax_rate", "is_active")
    readonly_fields = ("image_preview",)

    @admin.display(description="Photo", boolean=True)
    def has_photo(self, obj):
        return bool(obj.image)

    @admin.display(description="Current photo")
    def image_preview(self, obj):
        if obj and obj.image:
            return format_html('<img src="{}" style="height:90px;border-radius:8px">', obj.image.url)
        return "No photo — a themed tile is used."


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("product", "quantity", "status", "updated_at")
    list_editable = ("quantity",)
    search_fields = ("product__name",)

    @admin.display(description="Status")
    def status(self, obj):
        if obj.quantity <= 0:
            return "OUT OF STOCK"
        return "LOW" if obj.is_low else "OK"


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "created_at")
    search_fields = ("name", "phone")


class BillItemInline(admin.TabularInline):
    model = BillItem
    extra = 0
    readonly_fields = ("product_name", "quantity", "unit_price", "line_total")


class PaymentInline(admin.StackedInline):
    model = Payment
    extra = 0


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = ("bill_number", "created_at", "customer", "cashier", "total", "status")
    list_filter = ("status", "created_at")
    search_fields = ("bill_number", "customer__name")
    date_hierarchy = "created_at"
    inlines = [BillItemInline, PaymentInline]


@admin.register(BillItem)
class BillItemAdmin(admin.ModelAdmin):
    list_display = ("bill", "product_name", "quantity", "unit_price", "line_total")
    search_fields = ("bill__bill_number", "product_name")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("bill", "method", "amount", "reference", "paid_at")
    list_filter = ("method",)


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("session_key", "created_at", "updated_at")


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("cart", "product", "quantity")


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_person", "phone", "gst_number", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "contact_person", "phone", "gst_number")


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 1


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("reference", "supplier", "purchase_date", "total", "status",
                    "created_by")
    list_filter = ("status", "purchase_date", "supplier")
    search_fields = ("reference", "supplier__name")
    date_hierarchy = "purchase_date"
    inlines = [PurchaseItemInline]


@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = ("purchase", "product", "quantity", "unit_cost", "tax_rate",
                    "expiry_date", "line_total")
    search_fields = ("purchase__reference", "product__name")
