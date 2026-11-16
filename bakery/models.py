from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

ZERO = Decimal("0.00")

CENTS = Decimal("0.01")

#: A phone number is exactly this many digits - no more, no fewer.
PHONE_LENGTH = 10

#: On the model as well as the form, so the admin and direct ORM writes cannot
#: store a 9 or 11 digit number either.
phone_validator = RegexValidator(
    regex=r"^\d{%d}$" % PHONE_LENGTH,
    message="Phone number must be exactly %d digits." % PHONE_LENGTH,
)


def _round(value):
    """Money is rounded to paise, never left with a third decimal hanging."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)

#: A product is flagged as "expiring soon" this many days before its date.
EXPIRY_SOON_DAYS = 7

#: Fixed stock threshold: at or below this many units a product is low stock.
LOW_STOCK_THRESHOLD = 10

#: The fixed set of units a product may be sold in. Pick one from the list so
#: every unit on every screen reads the same ("kg" never becomes "KG"/"kgs").
UNIT_CHOICES = (
    ("pcs", "Piece (pcs)"),
    ("kg", "Kilogram (kg)"),
    ("g", "Gram (g)"),
    ("litre", "Litre (litre)"),
    ("ml", "Millilitre (ml)"),
    ("pack", "Pack"),
    ("box", "Box"),
    ("loaf", "Loaf"),
    ("dozen", "Dozen"),
    ("bag", "Bag"),
    ("bottle", "Bottle"),
    ("jar", "Jar"),
    ("can", "Can"),
    ("cup", "Cup"),
    ("glass", "Glass"),
    ("slice", "Slice"),
    ("bundle", "Bundle"),
)


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("5.00"),
        validators=[MinValueValidator(ZERO)],
        help_text="GST percentage applied to every product in this category.",
    )

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=150, unique=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=ZERO,
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal("100.00"))],
        help_text="Discount percentage applied automatically at checkout.",
    )
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES, default="pcs")
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("5.00"),
        validators=[MinValueValidator(ZERO)],
        help_text="Filled in from the category unless you override it here.",
    )
    expiry_date = models.DateField(
        blank=True, null=True,
        help_text="Leave blank for non-perishable items.",
    )
    image = models.ImageField(
        upload_to="products/", blank=True, null=True, help_text="Uploaded product photo."
    )
    image_url = models.CharField(
        max_length=300, blank=True, help_text="Optional. Used when no photo is uploaded."
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def stock(self):
        inv = getattr(self, "inventory", None)
        return inv.quantity if inv else 0

    @property
    def is_low_stock(self):
        return 0 < self.stock <= LOW_STOCK_THRESHOLD

    @property
    def is_out_of_stock(self):
        return self.stock <= 0

    @property
    def stock_status(self):
        if self.is_out_of_stock:
            return "out"
        if self.is_low_stock:
            return "low"
        return "ok"

    @property
    def stock_status_label(self):
        return {
            "out": "Out of Stock",
            "low": "Low Stock",
            "ok": "In Stock",
        }[self.stock_status]

    @property
    def low_stock_message(self):
        if self.is_out_of_stock:
            return "Out of Stock"
        if self.is_low_stock:
            return "Low Stock! Only %d unit%s remaining." % (
                self.stock, "" if self.stock == 1 else "s"
            )
        return "In Stock"

    @property
    def days_to_expiry(self):
        """Whole days until expiry. Negative once the date has passed, or
        None for a product that does not expire."""
        if self.expiry_date is None:
            return None
        return (self.expiry_date - timezone.localdate()).days

    @property
    def is_expired(self):
        days = self.days_to_expiry
        return days is not None and days < 0

    @property
    def is_expiring_soon(self):
        days = self.days_to_expiry
        return days is not None and 0 <= days <= EXPIRY_SOON_DAYS

    @property
    def expiry_state(self):
        """One of 'none' / 'expired' / 'soon' / 'ok', so a template can pick
        the pill colour without re-doing the date maths."""
        if self.expiry_date is None:
            return "none"
        days = self.days_to_expiry
        if days < 0:
            return "expired"
        if days <= EXPIRY_SOON_DAYS:
            return "soon"
        return "ok"

    @property
    def expiry_note(self):
        """Short human line for the expiry column, e.g. 'Expired 2 days ago'."""
        state = self.expiry_state
        if state == "none":
            return "No expiry"
        days = self.days_to_expiry
        if state == "expired":
            return "Expired %d day%s ago" % (abs(days), "" if abs(days) == 1 else "s")
        if days == 0:
            return "Expires today"
        if days == 1:
            return "Expires tomorrow"
        if state == "soon":
            return "%d days left" % days
        return "Valid"


class Inventory(models.Model):
    product = models.OneToOneField(
        Product, on_delete=models.CASCADE, related_name="inventory"
    )
    quantity = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField(default=10)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Inventories"

    def __str__(self):
        return f"{self.product.name} ({self.quantity})"

    @property
    def stock_value(self):
        """What the units on hand are worth at the selling price."""
        return _round(self.product.price * self.quantity)

    @property
    def is_out(self):
        return self.quantity <= 0

    @property
    def is_low(self):
        return 0 < self.quantity <= LOW_STOCK_THRESHOLD

    @property
    def stock_status(self):
        if self.is_out:
            return "out"
        if self.is_low:
            return "low"
        return "ok"

    @property
    def stock_status_label(self):
        return {
            "out": "Out of Stock",
            "low": "Low Stock",
            "ok": "In Stock",
        }[self.stock_status]

    @property
    def low_stock_message(self):
        if self.is_out:
            return "Out of Stock"
        if self.is_low:
            return "Low Stock! Only %d unit%s remaining." % (
                self.quantity, "" if self.quantity == 1 else "s"
            )
        return "In Stock"


class Customer(models.Model):
    name = models.CharField(max_length=150)
    phone = models.CharField(
        max_length=PHONE_LENGTH, unique=True, validators=[phone_validator],
        help_text="Exactly %d digits." % PHONE_LENGTH,
    )
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"


class Supplier(models.Model):
    """A vendor the shop buys stock from."""

    name = models.CharField(max_length=150, unique=True)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(
        max_length=PHONE_LENGTH, validators=[phone_validator],
        help_text="Exactly %d digits." % PHONE_LENGTH,
    )
    email = models.EmailField(blank=True)
    gst_number = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Suppliers"
        ordering = ["name"]

    DEFAULT_NAME = "Warana Milk"

    def __str__(self):
        return self.name

    @classmethod
    def default_supplier(cls):
        """The single active Warana Milk supplier, created on demand."""
        supplier = cls.objects.filter(name__iexact=cls.DEFAULT_NAME).first()
        if supplier is None:
            supplier = cls.objects.create(
                name=cls.DEFAULT_NAME,
                contact_person=cls.DEFAULT_NAME,
                phone="9876543210",
                address="Warana, Maharashtra",
            )
        elif not supplier.is_active:
            supplier.is_active = True
            supplier.save(update_fields=["is_active"])
        return supplier

    @property
    def purchase_total(self):
        return sum((p.total for p in self.purchases.all()), ZERO)


class Cart(models.Model):
    """One open cart per browser session."""

    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart {self.session_key}"

    @property
    def items(self):
        return self.cart_items.select_related("product", "product__inventory")

    @property
    def subtotal(self):
        return sum((i.line_total for i in self.items), ZERO)

    @property
    def tax_amount(self):
        return sum((i.line_tax for i in self.items), ZERO)

    @property
    def total(self):
        return _round(self.subtotal + self.tax_amount)

    @property
    def item_count(self):
        return sum(i.quantity for i in self.items)


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="cart_items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])

    class Meta:
        unique_together = ("cart", "product")

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"

    @property
    def line_total(self):
        return (self.product.price * self.quantity).quantize(Decimal("0.01"))

    @property
    def line_discount(self):
        return _round(self.line_total * self.product.discount_percent / Decimal("100"))

    @property
    def tax_rate(self):
        return self.product.tax_rate or ZERO

    @property
    def line_tax(self):
        return _round(self.line_total * self.tax_rate / Decimal("100"))

    @property
    def line_tax_after_discount(self):
        return _round(
            (self.line_total - self.line_discount)
            * self.tax_rate / Decimal("100")
        )

    @property
    def line_total_with_tax(self):
        return _round(self.line_total + self.line_tax)


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    CARD = "CARD", "Card"
    UPI = "UPI", "UPI"


class BillStatus(models.TextChoices):
    PAID = "PAID", "Paid"
    PENDING = "PENDING", "Pending"
    CANCELLED = "CANCELLED", "Cancelled"


class Bill(models.Model):
    bill_number = models.CharField(max_length=20, unique=True, db_index=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="bills"
    )
    cashier = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="bills"
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("5.00"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    status = models.CharField(
        max_length=10, choices=BillStatus.choices, default=BillStatus.PAID
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.bill_number

    @property
    def item_count(self):
        return sum(i.quantity for i in self.items.all())

    @property
    def payment(self):
        return getattr(self, "payment_info", None)


class BillItem(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    product_name = models.CharField(max_length=150)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    def __str__(self):
        return f"{self.quantity} x {self.product_name}"

    @property
    def line_total_with_tax(self):
        return _round(self.line_total + self.tax_amount)


class Payment(models.Model):
    bill = models.OneToOneField(Bill, on_delete=models.CASCADE, related_name="payment_info")
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=60, blank=True)
    paid_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.bill.bill_number} - {self.get_method_display()}"


class PurchaseStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    CANCELLED = "CANCELLED", "Cancelled"


class Purchase(models.Model):
    """Stock received from a supplier.

    `reference` is the supplier's own invoice number, so the same bill can
    never be keyed in twice. Saving a purchase raises stock; deleting one
    gives it back.
    """

    reference = models.CharField(
        max_length=40, unique=True, db_index=True,
        help_text="Supplier invoice number.",
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="purchases"
    )
    purchase_date = models.DateField(default=timezone.localdate)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    notes = models.TextField(blank=True)
    status = models.CharField(
        max_length=10, choices=PurchaseStatus.choices, default=PurchaseStatus.RECEIVED
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="purchases"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-purchase_date", "-id"]
        verbose_name_plural = "Purchases"

    def __str__(self):
        return f"{self.reference} - {self.supplier.name}"

    @property
    def unit_count(self):
        return sum(i.quantity for i in self.items.all())


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_cost = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    expiry_date = models.DateField(
        blank=True, null=True, help_text="Batch expiry, if the supplier gave one."
    )
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    class Meta:
        verbose_name_plural = "Purchase Items"

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"

    @property
    def line_tax(self):
        return _round(self.line_total * self.tax_rate / Decimal("100"))

    @property
    def line_total_with_tax(self):
        return _round(self.line_total + self.line_tax)
