"""Form definitions and shared validation helpers.

Everything that a user can type in is checked here and on the models, and the
templates render both the field errors and `non_field_errors` so no message is
ever swallowed. Inputs that arrive through a form (browse, cart, checkout,
products, categories, suppliers, purchases) all go through validation before
anything is saved or stock is changed.
"""

import json
import re
import urllib.parse
from datetime import datetime
from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms.models import inlineformset_factory
from django.utils import timezone

from .models import (PHONE_LENGTH, Category, Customer, Inventory,
                     PaymentMethod, Product, Purchase, PurchaseItem, Supplier)

PHONE_WIDGET = {
    "class": "input",
    "inputmode": "numeric",
    "maxlength": str(PHONE_LENGTH),
    "placeholder": "10 digits",
    "autocomplete": "off",
}


def clean_phone(raw, label="Phone number"):
    """Return `raw` as exactly 10 digits, or raise a readable error.

    Spaces, dashes, brackets and a leading +91 / 0 are dropped first, because
    that is how people actually write a number; anything else left over is a
    real typo rather than formatting. The length is then checked so both a
    short and a long number get a message that says which it is.
    """
    raw = (raw or "").strip()
    stripped = re.sub(r"[\s\-+()]", "", raw)

    if not stripped:
        raise ValidationError("%s is required." % label)
    if not stripped.isdigit():
        raise ValidationError(
            "%s must contain digits only - letters and symbols are not allowed."
            % label
        )

    digits = stripped
    if digits.startswith("91") and len(digits) > PHONE_LENGTH:
        digits = digits[-PHONE_LENGTH:]  # +91 98765 43210 -> 9876543210
    elif digits.startswith("0") and len(digits) > PHONE_LENGTH:
        digits = digits[-PHONE_LENGTH:]  # 098765 43210     -> 9876543210

    if len(digits) != PHONE_LENGTH:
        raise ValidationError(
            "%s must be exactly %d digits - '%s' has %d digit%s."
            % (label, PHONE_LENGTH, raw or stripped, len(digits),
               "" if len(digits) == 1 else "s")
        )
    return digits


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "Username", "autofocus": True})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "input", "placeholder": "Password"})
    )


class ResetPasswordForm(forms.Form):
    """The "Forgot Username / Password?" popup, step 2.

    `username` is the account that was verified in step 1; `new_username`
    lets the same account be renamed (blank keeps the old name). The account
    lookup and the actual update happen in the view; this only checks that
    the two password boxes agree.
    """

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "Username",
                   "autocomplete": "username"}
        ),
    )
    new_username = forms.CharField(
        required=False, max_length=150, label="New username",
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "New username (blank = keep current)",
                   "autocomplete": "username"}
        ),
    )
    new_password = forms.CharField(
        label="New password", strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "placeholder": "New password",
                   "autocomplete": "new-password"}
        ),
    )
    confirm_password = forms.CharField(
        label="Confirm new password", strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "placeholder": "Re-enter new password",
                   "autocomplete": "new-password"}
        ),
    )

    def clean_username(self):
        return (self.cleaned_data.get("username") or "").strip()

    def clean_new_username(self):
        return (self.cleaned_data.get("new_username") or "").strip()

    def clean(self):
        data = super().clean()
        new = data.get("new_password")
        confirm = data.get("confirm_password")
        if new and confirm and new != confirm:
            self.add_error("confirm_password", "The two passwords do not match.")
        return data


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "description", "tax_rate"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 2}),
            "tax_rate": forms.NumberInput(
                attrs={"class": "input", "step": "0.01", "min": "0", "max": "100"}
            ),
        }

    def clean_tax_rate(self):
        return _tax_rate_in_range(self.cleaned_data.get("tax_rate"))


class ProductForm(forms.ModelForm):
    # Declared by hand so the field is optional: when it is left blank the
    # category's own rate is used, which is what "tax follows the category"
    # means in practice.
    tax_rate = forms.DecimalField(
        required=False, label="GST %",
        widget=forms.NumberInput(
            attrs={"class": "input", "step": "0.01", "min": "0", "max": "100"}
        ),
        help_text="Each product can have its own GST rate. Taken from the category when left blank.",
    )
    discount_percent = forms.DecimalField(
        required=False, initial=Decimal("0"), min_value=Decimal("0"),
        max_value=Decimal("100"), label="Discount %",
        widget=forms.NumberInput(
            attrs={"class": "input", "step": "0.01", "min": "0", "max": "100"}
        ),
        help_text="Automatically deducted from this product's price at checkout.",
    )
    initial_stock = forms.IntegerField(
        required=False, min_value=0, label="Opening stock",
        widget=forms.NumberInput(attrs={"class": "input"}),
    )
    reorder_level = forms.IntegerField(
        required=False, min_value=0, initial=10, label="Reorder level",
        widget=forms.NumberInput(attrs={"class": "input"}),
    )

    class Meta:
        model = Product
        fields = ["name", "category", "price", "unit", "tax_rate", "discount_percent",
                  "expiry_date", "image", "image_url", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "category": forms.Select(attrs={"class": "input"}),
            "price": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": "0"}),
            "unit": forms.Select(attrs={"class": "input"}),
            "expiry_date": forms.DateInput(
                attrs={"class": "input", "type": "date"}, format="%Y-%m-%d"
            ),
            "image": forms.ClearableFileInput(attrs={"class": "input"}),
            "image_url": forms.URLInput(attrs={"class": "input", "placeholder": "https://... (optional)"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["expiry_date"].input_formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
        # Let the page auto-fill the rate when a category is picked, without
        # a second AJAX round trip.
        tax_map = {
            str(pk): str(rate)
            for pk, rate in Category.objects.values_list("pk", "tax_rate")
        }
        self.fields["category"].widget.attrs["data-tax-map"] = json.dumps(tax_map)
        if not self.is_bound and self.instance.pk is None:
            self.fields["tax_rate"].initial = ""

    def clean_tax_rate(self):
        return _tax_rate_in_range(self.cleaned_data.get("tax_rate"))

    def clean_image_url(self):
        value = (self.cleaned_data.get("image_url") or "").strip()
        if value:
            parts = urllib.parse.urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValidationError(
                    "Image URL must start with http:// or https:// and name a host."
                )
        return value

    def clean(self):
        data = super().clean()
        category = data.get("category")
        if data.get("tax_rate") is None and category is not None:
            # Requirement: tax follows the category unless deliberately set.
            data["tax_rate"] = category.tax_rate
        if data.get("discount_percent") is None:
            data["discount_percent"] = Decimal("0")
        return data

    def save(self, commit=True):
        product = super().save(commit=False)
        if commit:
            product.save()
            stock = self.cleaned_data.get("initial_stock")
            reorder = self.cleaned_data.get("reorder_level")
            inv, _ = Inventory.objects.get_or_create(product=product)
            if stock is not None:
                inv.quantity = stock
            if reorder is not None:
                inv.reorder_level = reorder
            inv.save()
        return product


def _tax_rate_in_range(value):
    """Shared 0-100 check for the several tax fields in this module."""
    if value is None:
        return None
    if value < 0 or value > 100:
        raise ValidationError("Tax must be between 0 and 100 percent.")
    return value


#: 27 AAACV 1234 A 1 Z 5 - state, PAN, serial, and the mandatory Z checksum
#: letter that every Indian GSTIN ends with.
GST_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


def clean_gst_number(raw, label="GSTIN"):
    """Trim, capitalise and shape-check a GST number. Blank is fine, anything
    else has to look like a real 15-character GSTIN."""
    value = (raw or "").strip().upper()
    if not value:
        return ""
    if not GST_PATTERN.match(value):
        raise ValidationError(
            "%s must be 15 characters in the format e.g. 27AAACV1234A1Z5."
            % label
        )
    return value


class StockForm(forms.ModelForm):
    class Meta:
        model = Inventory
        fields = ["quantity", "reorder_level"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"class": "input", "min": "0"}),
            "reorder_level": forms.NumberInput(attrs={"class": "input", "min": "0"}),
        }


class CustomerForm(forms.ModelForm):
    # Raw input is limited to 25 characters so a country code can be typed;
    # clean_phone() reduces it to the 10 digits actually stored.
    phone = forms.CharField(
        max_length=25, label="Phone", widget=forms.TextInput(attrs=PHONE_WIDGET)
    )

    class Meta:
        model = Customer
        fields = ["name", "phone", "email", "address"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "email": forms.EmailInput(attrs={"class": "input"}),
            "address": forms.Textarea(attrs={"class": "input", "rows": 2}),
        }

    def clean_phone(self):
        return clean_phone(self.cleaned_data.get("phone"))


class SupplierForm(forms.ModelForm):
    phone = forms.CharField(
        max_length=25, label="Phone", widget=forms.TextInput(attrs=PHONE_WIDGET)
    )

    class Meta:
        model = Supplier
        fields = ["name", "contact_person", "phone", "email", "gst_number",
                  "address", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "contact_person": forms.TextInput(attrs={"class": "input"}),
            "email": forms.EmailInput(attrs={"class": "input"}),
            "gst_number": forms.TextInput(attrs={"class": "input", "maxlength": "20"}),
            "address": forms.Textarea(attrs={"class": "input", "rows": 2}),
        }

    def clean_phone(self):
        return clean_phone(self.cleaned_data.get("phone"))

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            return name
        clash = Supplier.objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("A supplier named %s already exists." % name)
        is_default = name.casefold() == Supplier.DEFAULT_NAME.casefold()
        was_default = (
            self.instance.pk
            and self.instance.name.casefold() == Supplier.DEFAULT_NAME.casefold()
        )
        if is_default and not was_default:
            raise ValidationError(
                "Warana Milk is the default supplier and already exists - edit it instead."
            )
        if was_default and not is_default:
            raise ValidationError("The default supplier must stay named Warana Milk.")
        return name

    def clean_gst_number(self):
        return clean_gst_number(self.cleaned_data.get("gst_number"))


class PurchaseForm(forms.ModelForm):
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.none(),
        label="Purchase From (Supplier)",
        empty_label="Select a supplier",
        widget=forms.Select(attrs={"class": "input"}),
    )

    class Meta:
        model = Purchase
        fields = ["supplier", "reference", "purchase_date", "notes"]
        widgets = {
            "reference": forms.TextInput(
                attrs={"class": "input", "maxlength": "40",
                      "placeholder": "Supplier invoice number"}
            ),
            "purchase_date": forms.DateInput(
                attrs={"class": "input", "type": "date"}, format="%Y-%m-%d"
            ),
            "notes": forms.Textarea(attrs={"class": "input", "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["purchase_date"].input_formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
        if not self.is_bound:
            self.fields["purchase_date"].initial = timezone.localdate()
        supplier_filter = Q(is_active=True)
        if self.instance.pk and self.instance.supplier_id:
            supplier_filter |= Q(pk=self.instance.supplier_id)
        suppliers = Supplier.objects.filter(supplier_filter)
        self.fields["supplier"].queryset = suppliers
        if not self.is_bound and not self.instance.pk:
            default = suppliers.filter(name__iexact=Supplier.DEFAULT_NAME).first()
            if default is None:
                default = Supplier.default_supplier()
                self.fields["supplier"].queryset = Supplier.objects.filter(
                    Q(is_active=True) | Q(pk=default.pk)
                )
            self.fields["supplier"].initial = default.pk

    def clean_purchase_date(self):
        value = self.cleaned_data.get("purchase_date")
        if value and value > timezone.localdate():
            raise ValidationError(
                "Purchase date cannot be in the future (entered %s)."
                % value.strftime("%d %b %Y")
            )
        return value

    def clean_reference(self):
        value = (self.cleaned_data.get("reference") or "").strip()
        if not value:
            raise ValidationError("Supplier invoice number is required.")
        clash = Purchase.objects.filter(reference__iexact=value)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError(
                "Invoice number %s has already been recorded - check the "
                "purchase list before entering it again." % value
            )
        return value

    def clean_supplier(self):
        supplier = self.cleaned_data.get("supplier")
        if supplier is not None and not supplier.is_active:
            raise ValidationError(
                "%s is marked inactive - make it active before buying from it."
                % supplier.name
            )
        return supplier


class PurchaseItemForm(forms.ModelForm):
    tax_rate = forms.DecimalField(
        required=False, label="Tax %",
        widget=forms.NumberInput(
            attrs={"class": "input", "step": "0.01", "min": "0", "max": "100"}
        ),
        help_text="Left blank, the product's own tax rate is used.",
    )

    class Meta:
        model = PurchaseItem
        fields = ["product", "quantity", "unit_cost", "tax_rate", "expiry_date"]
        widgets = {
            "product": forms.Select(attrs={"class": "input"}),
            "quantity": forms.NumberInput(attrs={"class": "input", "min": "1", "step": "1"}),
            "unit_cost": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": "0"}),
            "expiry_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
        }

    def __init__(self, *args, purchase_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The purchase form decides the date before the lines are validated,
        # and passes it in so a batch cannot be dated before its own purchase.
        # Tolerate a string so a badly wired caller cannot crash the form.
        if isinstance(purchase_date, str):
            try:
                purchase_date = datetime.strptime(purchase_date, "%Y-%m-%d").date()
            except ValueError:
                purchase_date = None
        self.purchase_date = purchase_date
        self.fields["expiry_date"].required = False
        self.fields["expiry_date"].input_formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
        self.fields["product"].queryset = (
            Product.objects.filter(is_active=True).select_related("category")
        )
        self.fields["product"].empty_label = "Select a product"
        self.fields["product"].widget.attrs["data-tax-map"] = json.dumps({
            str(pk): str(rate)
            for pk, rate in Product.objects.values_list("pk", "tax_rate")
        })

    def clean_tax_rate(self):
        return _tax_rate_in_range(self.cleaned_data.get("tax_rate"))

    def clean_expiry_date(self):
        value = self.cleaned_data.get("expiry_date")
        if value and self.purchase_date and value < self.purchase_date:
            raise ValidationError(
                "Expiry date (%s) is before the purchase date (%s)."
                % (value.strftime("%d %b %Y"), self.purchase_date.strftime("%d %b %Y"))
            )
        return value

    def clean(self):
        data = super().clean()
        product = data.get("product")
        if data.get("tax_rate") is None and product is not None:
            data["tax_rate"] = product.tax_rate or Decimal("0.00")
        return data


class BasePurchaseItemFormSet(forms.BaseInlineFormSet):
    """Purchase-line rules that need the whole formset to decide."""

    def clean(self):
        super().clean()
        if any(self.errors):
            # Row-level messages are more useful than a second complaint about
            # the same rows, so stop once something is already wrong.
            return

        seen = set()
        kept = False
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            product = form.cleaned_data.get("product")
            if product is None:
                continue
            kept = True
            if product.pk in seen:
                form.add_error(
                    "product",
                    "%s appears on more than one line - combine the quantities."
                    % product.name,
                )
            seen.add(product.pk)

        if not kept:
            raise ValidationError("Add at least one product line to the purchase.")


PurchaseItemFormSet = inlineformset_factory(
    Purchase, PurchaseItem, form=PurchaseItemForm, formset=BasePurchaseItemFormSet,
    extra=3, can_delete=True,
)


class CheckoutForm(forms.Form):
    customer_name = forms.CharField(
        required=False, max_length=150, label="Customer name",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "Walk-in customer"}),
    )
    customer_phone = forms.CharField(
        required=False, max_length=25, label="Phone",
        widget=forms.TextInput(dict(PHONE_WIDGET, placeholder="10 digits (optional)")),
    )
    discount = forms.DecimalField(
        required=False, initial=Decimal("0"), min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": "0"}),
    )
    method = forms.ChoiceField(
        choices=PaymentMethod.choices,
        widget=forms.Select(attrs={"class": "input"}),
    )
    reference = forms.CharField(
        required=False, max_length=60, label="Card last 4 / UPI ID",
        widget=forms.TextInput(attrs={"class": "input"}),
    )

    def clean_customer_phone(self):
        value = (self.cleaned_data.get("customer_phone") or "").strip()
        if value:
            # Blank is allowed (walk-in), but a number that is typed has to be
            # exactly 10 digits. Raised here so the message sits on the phone
            # field, not in the page-level non-field errors.
            return clean_phone(value, "Customer phone")
        return None

    def clean(self):
        data = super().clean()
        name = (data.get("customer_name") or "").strip()
        phone = data.get("customer_phone") or ""
        if bool(name) != bool(phone):
            raise forms.ValidationError("Enter both customer name and phone, or leave both blank.")
        data["customer_name"] = name
        data["customer_phone"] = phone
        return data
