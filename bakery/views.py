import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import password_validation
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, F, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import billing, exports, purchasing
from .forms import (CategoryForm, CheckoutForm, CustomerForm, LoginForm,
                    ProductForm, PurchaseForm, PurchaseItemFormSet,
                    ResetPasswordForm, StockForm, SupplierForm)
from .models import (EXPIRY_SOON_DAYS, LOW_STOCK_THRESHOLD, Bill, BillItem,
                     Cart, CartItem, Category, Customer, Inventory, Payment,
                     Product, Purchase, PurchaseItem, PurchaseStatus, Supplier)

# ---------------------------------------------------------------- auth

class UserLoginView(LoginView):
    template_name = "bakery/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        # "Remember me" -> keep the session for two weeks instead of expiring
        # when the browser closes.
        response = super().form_valid(form)
        if not self.request.POST.get("remember"):
            self.request.session.set_expiry(0)
        else:
            self.request.session.set_expiry(60 * 60 * 24 * 14)
        return response

    def get_success_url(self):
        # Never redirect to an arbitrary host: an unvalidated ?next= is an
        # open redirect (a crafted login link could bounce a just
        # authenticated user to an attacker's site).
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return next_url
        return "/"


class UserLogoutView(LogoutView):
    next_page = "login"


@require_POST
def check_username(request):
    """Step 1 of the reset popup: confirm the typed username really exists
    in the database before letting anyone type a new password."""
    username = (request.POST.get("username") or "").strip()
    if not username:
        return JsonResponse(
            {"ok": False, "error": "Enter a username to check."}, status=400
        )

    user = get_user_model().objects.filter(username__iexact=username).first()
    if user is None:
        return JsonResponse(
            {"ok": False,
             "error": "No account found with username '%s' in the database." % username},
            status=400,
        )

    return JsonResponse({
        "ok": True,
        "username": user.username,
        "message": "Username '%s' found in the database. You can now set a new username and password." % user.username,
    })


@require_POST
def reset_password(request):
    """Step 2: rename the account and/or update its password in the database.

    The login page sends this over AJAX and renders the JSON message inside
    its own popup; a plain form post (no JS) falls back to a Django message
    and lands back on the login page.
    """
    form = ResetPasswordForm(request.POST)
    if not form.is_valid():
        error = " ".join(
            str(e) for errs in form.errors.values() for e in errs
        )
        return JsonResponse({"ok": False, "error": error}, status=400)

    username = form.cleaned_data["username"]
    user = get_user_model().objects.filter(username__iexact=username).first()
    if user is None:
        return JsonResponse(
            {"ok": False,
             "error": "No account found with username '%s'." % username},
            status=400,
        )

    new_username = form.cleaned_data["new_username"] or username
    if new_username.lower() != user.username.lower() and (
        get_user_model().objects.filter(username__iexact=new_username)
        .exclude(pk=user.pk).exists()
    ):
        return JsonResponse(
            {"ok": False,
             "error": "Username '%s' is already taken - choose another." % new_username},
            status=400,
        )

    new_password = form.cleaned_data["new_password"]
    try:
        password_validation.validate_password(new_password, user)
    except ValidationError as exc:
        return JsonResponse(
            {"ok": False, "error": " ".join(str(e) for e in exc)},
            status=400,
        )

    user.username = new_username
    user.set_password(new_password)                  # hashes it
    user.save(update_fields=["username", "password"])  # writes it to the database
    message = (
        "Username '%s' and password have been updated in the database. "
        "Please login with your new details." % new_username
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "message": message})

    messages.success(request, message)
    return redirect("login")


# ---------------------------------------------------------------- helpers

def _get_cart(request):
    if not request.session.session_key:
        request.session.save()
    cart, _ = Cart.objects.get_or_create(session_key=request.session.session_key)
    return cart


def _redirect_back(request, fallback):
    """Return to the page that posted the cart form, when it is safe."""
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(target)
    return redirect(fallback)


def _dashboard_counts():
    today = timezone.localdate()
    bills_today = Bill.objects.filter(created_at__date=today, status="PAID")
    revenue = bills_today.aggregate(v=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField()))["v"]
    soon = timezone.localdate() + timedelta(days=EXPIRY_SOON_DAYS)
    dated = Product.objects.filter(is_active=True, expiry_date__isnull=False)
    return {
        "product_count": Product.objects.filter(is_active=True).count(),
        "stock_count": Inventory.objects.aggregate(v=Coalesce(Sum("quantity"), 0))["v"],
        "low_stock_count": Inventory.objects.filter(
            quantity__gt=0, quantity__lte=LOW_STOCK_THRESHOLD
        ).count(),
        "out_of_stock_count": Inventory.objects.filter(quantity__lte=0).count(),
        "bill_count": Bill.objects.count(),
        "bills_today": bills_today.count(),
        "sales_today": revenue,
        "customer_count": Customer.objects.count(),
        "expired_count": dated.filter(expiry_date__lt=today).count(),
        "expiring_count": dated.filter(expiry_date__gte=today, expiry_date__lte=soon).count(),
        "supplier_count": Supplier.objects.filter(is_active=True).count(),
    }


# ---------------------------------------------------------------- pages

def _last_n_days(n=7):
    """Zero-filled list of {'day', 'bills', 'net'} for the last n days, oldest first."""
    today = timezone.localdate()
    start = today - timedelta(days=n - 1)
    totals = DecimalField()
    rows = (
        Bill.objects.filter(created_at__date__gte=start, status="PAID")
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(bills=Count("id"), net=Coalesce(Sum("total"), Value(Decimal("0")), output_field=totals))
        .order_by("day")
    )
    by_day = {r["day"]: r for r in rows}

    series = []
    for offset in range(n):
        day = start + timedelta(days=offset)
        hit = by_day.get(day)
        series.append({
            "day": day,
            "bills": hit["bills"] if hit else 0,
            "net": hit["net"] if hit else Decimal("0"),
        })
    return series


@login_required
def dashboard(request):
    recent = Bill.objects.select_related("customer", "payment_info")[:8]
    low_stock = Inventory.objects.select_related("product").filter(
        quantity__lte=LOW_STOCK_THRESHOLD
    ).order_by("quantity")
    today = timezone.localdate()
    soon = today + timedelta(days=EXPIRY_SOON_DAYS)
    expiring = (
        Product.objects.select_related("inventory")
        .filter(is_active=True, expiry_date__isnull=False, expiry_date__lte=soon)
        .order_by("expiry_date")[:6]
    )
    return render(request, "bakery/dashboard.html", {
        "counts": _dashboard_counts(),
        "recent_bills": recent,
        "low_stock": low_stock,
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
        "expiring": expiring,
        "week": _last_n_days(7),
        "top_products": (
            BillItem.objects.values("product_name")
            .annotate(units=Sum("quantity"), revenue=Sum("line_total"))
            .order_by("-units")[:5]
        ),
    })


@login_required
def product_list(request):
    products = Product.objects.select_related("category", "inventory").order_by("name")
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    expiry = request.GET.get("expiry", "").strip()
    if q:
        products = products.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if category:
        products = products.filter(category_id=category)
    if expiry in ("expired", "soon", "dated"):
        today = timezone.localdate()
        soon = today + timedelta(days=EXPIRY_SOON_DAYS)
        if expiry == "dated":
            products = products.filter(expiry_date__isnull=False)
        elif expiry == "expired":
            products = products.filter(expiry_date__lt=today)
        else:
            products = products.filter(expiry_date__gte=today, expiry_date__lte=soon)
    return render(request, "bakery/products.html", {
        "products": products,
        "categories": Category.objects.all(),
        "category_rows": Category.objects.annotate(
            product_count=Count("products")
        ).order_by("name"),
        "q": q,
        "selected_category": category,
        "selected_expiry": expiry,
        "form": ProductForm(),
        "category_form": CategoryForm(),
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
    })


@login_required
def product_create(request):
    form = ProductForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Product added.")
        return redirect("bakery:products")
    return render(request, "bakery/product_form.html", {"form": form, "title": "Add product"})


@login_required
def product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = ProductForm(request.POST or None, request.FILES or None, instance=product)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Product updated.")
        return redirect("bakery:products")
    return render(request, "bakery/product_form.html", {"form": form, "product": product, "title": "Edit product"})


@login_required
@require_POST
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if BillItem.objects.filter(product=product).exists():
        product.is_active = False
        product.save(update_fields=["is_active"])
        messages.warning(request, f"{product.name} is used in old bills, so it was deactivated instead of deleted.")
    else:
        product.delete()
        messages.success(request, "Product deleted.")
    return redirect("bakery:products")


@login_required
def category_create(request):
    form = CategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Category added.")
    else:
        messages.error(request, "Could not save category.")
    return redirect("bakery:products")


# ---------------------------------------------------------------- suppliers

@login_required
def supplier_list(request):
    if request.method == "POST":
        # The "quick add" form on this page. Errors go back as a message,
        # the same way category_create does, so the list stays in view.
        form = SupplierForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Supplier added.")
        else:
            messages.error(request, " ".join(
                str(e) for errors in form.errors.values() for e in errors
            ))
        return redirect("bakery:suppliers")

    suppliers = Supplier.objects.all()
    show_all = request.GET.get("all") == "1"
    if not show_all:
        suppliers = suppliers.filter(is_active=True)
    rows = suppliers.annotate(
        purchase_count=Count("purchases"),
        spend=Coalesce(Sum("purchases__total"), Value(Decimal("0")),
                       output_field=DecimalField()),
    ).order_by("name")
    return render(request, "bakery/suppliers.html", {
        "suppliers": rows,
        "form": SupplierForm(),
        "show_all": show_all,
    })


@login_required
def supplier_create(request):
    form = SupplierForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Supplier added.")
        return redirect("bakery:suppliers")
    return render(request, "bakery/supplier_form.html", {"form": form, "title": "Add Supplier"})


@login_required
def supplier_edit(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    form = SupplierForm(request.POST or None, instance=supplier)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Supplier updated.")
        return redirect("bakery:suppliers")
    return render(request, "bakery/supplier_form.html", {
        "form": form, "supplier": supplier, "title": "Edit Supplier",
    })


@login_required
@require_POST
def supplier_delete(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    if supplier.name.casefold() == Supplier.DEFAULT_NAME.casefold():
        messages.warning(request, "Warana Milk is the default supplier and cannot be deleted.")
        return redirect("bakery:suppliers")
    if supplier.purchases.exists():
        # Purchases are the audit trail, so the supplier is retired instead.
        supplier.is_active = False
        supplier.save(update_fields=["is_active"])
        messages.warning(
            request,
            f"{supplier.name} has recorded purchases, so it was deactivated instead of deleted.",
        )
    else:
        supplier.delete()
        messages.success(request, "Supplier deleted.")
    return redirect("bakery:suppliers")


# ---------------------------------------------------------------- purchases

def _purchase_lines(formset):
    """Flatten a validated line formset into what purchasing.record_purchase
    wants, skipping the blank extra rows."""
    lines = []
    for form in formset.forms:
        data = getattr(form, "cleaned_data", None)
        if not data or data.get("DELETE"):
            continue
        lines.append({
            "item": form.instance if form.instance.pk else None,
            "product": data["product"],
            "quantity": data["quantity"],
            "unit_cost": data["unit_cost"],
            "tax_rate": data.get("tax_rate") or Decimal("0.00"),
            "expiry_date": data.get("expiry_date"),
        })
    return lines


def _purchase_form_kwargs(form):
    """The line forms need the posted purchase date so a batch cannot be dated
    before the day it was bought. Only a valid parent form knows it, so pass
    None when it failed - the parent's own errors are what matter then."""
    if form.is_valid():
        return {"purchase_date": form.cleaned_data.get("purchase_date")}
    return {}


@login_required
def purchase_list(request):
    rows = Purchase.objects.select_related("supplier", "created_by").annotate(
        units=Coalesce(Sum("items__quantity"), Value(0)),
        lines=Count("items"),
    )
    q = request.GET.get("q", "").strip()
    supplier = request.GET.get("supplier", "").strip()
    if q:
        rows = rows.filter(
            Q(reference__icontains=q) | Q(supplier__name__icontains=q)
        )
    if supplier:
        rows = rows.filter(supplier_id=supplier)
    return render(request, "bakery/purchases.html", {
        "purchases": rows,
        "suppliers": Supplier.objects.all(),
        "q": q,
        "selected_supplier": supplier,
        "month_total": Purchase.objects.aggregate(
            v=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField())
        )["v"],
    })


@login_required
def purchase_create(request):
    if request.method == "POST":
        form = PurchaseForm(request.POST)
        formset = PurchaseItemFormSet(
            request.POST,
            form_kwargs=_purchase_form_kwargs(form),
        )
        if form.is_valid() and formset.is_valid():
            try:
                purchase = purchasing.record_purchase(
                    reference=form.cleaned_data["reference"],
                    supplier=form.cleaned_data["supplier"],
                    purchase_date=form.cleaned_data["purchase_date"],
                    notes=form.cleaned_data.get("notes", ""),
                    created_by=request.user,
                    lines=_purchase_lines(formset),
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Purchase {purchase.reference} saved - "
                    f"{purchase.unit_count} unit(s) added to stock.",
                )
                return redirect("bakery:purchases")
    else:
        form = PurchaseForm()
        formset = PurchaseItemFormSet()
    return render(request, "bakery/purchase_form.html", {
        "form": form, "formset": formset, "title": "Record Purchase",
    })


@login_required
def purchase_edit(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    if request.method == "POST":
        form = PurchaseForm(request.POST, instance=purchase)
        formset = PurchaseItemFormSet(
            request.POST, instance=purchase,
            form_kwargs=_purchase_form_kwargs(form),
        )
        if form.is_valid() and formset.is_valid():
            try:
                updated = purchasing.update_purchase(
                    purchase,
                    reference=form.cleaned_data["reference"],
                    supplier=form.cleaned_data["supplier"],
                    purchase_date=form.cleaned_data["purchase_date"],
                    notes=form.cleaned_data.get("notes", ""),
                    lines=_purchase_lines(formset),
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Purchase {updated.reference} updated.")
                return redirect("bakery:purchases")
    else:
        form = PurchaseForm(instance=purchase)
        formset = PurchaseItemFormSet(instance=purchase)
    return render(request, "bakery/purchase_form.html", {
        "form": form, "formset": formset, "purchase": purchase,
        "title": "Edit Purchase",
    })


@login_required
@require_POST
def purchase_delete(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related("supplier"), pk=pk)
    reference = purchase.reference
    was_received = purchase.status == PurchaseStatus.RECEIVED
    if was_received:
        purchasing.reverse_stock(purchase)
    purchase.delete()
    if was_received:
        messages.success(
            request, f"Purchase {reference} removed and its stock taken back out."
        )
    else:
        messages.success(request, f"Purchase {reference} removed.")
    return redirect("bakery:purchases")


@login_required
def purchase_detail(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier", "created_by"), pk=pk
    )
    return render(request, "bakery/purchase_detail.html", {"purchase": purchase})


@login_required
def inventory(request):
    rows = Inventory.objects.select_related("product", "product__category").order_by("quantity")
    if request.method == "POST":
        form = StockForm(request.POST)
        if form.is_valid():
            inv = get_object_or_404(Inventory, pk=form.data.get("pk"))
            form = StockForm(request.POST, instance=inv)
            if form.is_valid():
                form.save()
                messages.success(request, f"Stock updated for {inv.product.name}.")
                return redirect("bakery:inventory")
        else:
            messages.error(request, "Invalid stock value.")
    return render(request, "bakery/inventory.html", {
        "rows": rows,
        "low_count": Inventory.objects.filter(
            quantity__gt=0, quantity__lte=LOW_STOCK_THRESHOLD
        ).count(),
        "out_count": Inventory.objects.filter(quantity__lte=0).count(),
        "total_units": Inventory.objects.aggregate(v=Coalesce(Sum("quantity"), 0))["v"],
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
    })


# ---------------------------------------------------------------- cart / billing

@login_required
def browse(request):
    products = (
        Product.objects.select_related("category", "inventory")
        .filter(is_active=True)
        .order_by("category__name", "name")
    )
    category = request.GET.get("category", "").strip()
    if category:
        products = products.filter(category_id=category)
    cart = _get_cart(request)
    return render(request, "bakery/browse.html", {
        "products": products,
        "categories": Category.objects.all(),
        "selected_category": category,
        "cart": cart,
    })


def _parse_qty(raw, default=1):
    """Parse a posted quantity, tolerating junk instead of raising a 500."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@login_required
@require_POST
def cart_add(request, pk):
    product = get_object_or_404(Product, pk=pk, is_active=True)
    available = product.stock
    if available < 1:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": f"{product.name} is out of stock."}, status=400)
        messages.error(request, f"{product.name} is out of stock.")
        return redirect("bakery:browse")

    qty = min(max(1, _parse_qty(request.POST.get("quantity"))), available)

    cart = _get_cart(request)
    item, created = CartItem.objects.get_or_create(
        cart=cart, product=product, defaults={"quantity": qty}
    )
    if not created:
        item.quantity = min(item.quantity + qty, available)
        item.save(update_fields=["quantity"])

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "ok": True,
            "cart_count": cart.item_count,
            "cart_total": str(cart.subtotal),
            "cart_tax": str(cart.tax_amount),
            "cart_grand_total": str(cart.total),
        })
    messages.success(request, f"{product.name} added to cart.")
    return _redirect_back(request, "bakery:browse")


@login_required
@require_POST
def cart_update(request, item_id):
    cart = _get_cart(request)
    item = get_object_or_404(
        CartItem.objects.select_related("product", "product__inventory"),
        pk=item_id, cart=cart,
    )
    qty = _parse_qty(request.POST.get("quantity"), default=item.quantity)
    if qty <= 0:
        name = item.product.name
        item.delete()
        messages.info(request, f"{name} removed from cart.")
        return _redirect_back(request, "bakery:cart")

    available = item.product.stock
    if available < 1:
        name = item.product.name
        item.delete()
        messages.error(request, f"{name} is out of stock and was removed from the cart.")
        return _redirect_back(request, "bakery:cart")
    if qty > available:
        messages.warning(
            request, f"Only {available} left of {item.product.name}; quantity adjusted."
        )
    item.quantity = min(qty, available)
    item.save(update_fields=["quantity"])
    return _redirect_back(request, "bakery:cart")


@login_required
@require_POST
def cart_remove(request, item_id):
    cart = _get_cart(request)
    get_object_or_404(CartItem, pk=item_id, cart=cart).delete()
    return _redirect_back(request, "bakery:cart")


@login_required
def cart(request):
    cart_obj = _get_cart(request)
    return render(request, "bakery/cart.html", {"cart": cart_obj})


@login_required
def checkout(request):
    cart_obj = _get_cart(request)
    form = CheckoutForm(request.POST or None, initial={"method": "CASH"})
    if request.method == "POST" and form.is_valid():
        customer = None
        if form.cleaned_data["customer_name"]:
            customer, _ = Customer.objects.get_or_create(
                phone=form.cleaned_data["customer_phone"],
                defaults={"name": form.cleaned_data["customer_name"]},
            )
            customer.name = form.cleaned_data["customer_name"]
            customer.save(update_fields=["name"])
        try:
            bill = billing.checkout(
                cart_obj,
                cashier=request.user,
                customer=customer,
                method=form.cleaned_data["method"],
                reference=form.cleaned_data.get("reference", ""),
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        except IntegrityError:
            messages.error(
                request,
                "Another till created a bill at the same moment. Please try again.",
            )
        else:
            messages.success(request, f"Bill {bill.bill_number} generated.")
            return redirect("bakery:receipt", pk=bill.pk)
    totals = billing.compute_product_discount_totals(cart_obj.items)
    return render(request, "bakery/checkout.html", {
        "cart": cart_obj, "form": form,
        "subtotal": totals[0], "discount": totals[1],
        "tax_amount": totals[2], "total": totals[3],
    })


@login_required
def receipt(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related("customer", "cashier", "payment_info"), pk=pk
    )
    return render(request, "bakery/receipt.html", {"bill": bill})


@login_required
def receipt_pdf(request, pk):
    """Download the receipt as a PDF, built on the fly from the same bill."""
    bill = get_object_or_404(
        Bill.objects.select_related("customer", "cashier", "payment_info"), pk=pk
    )
    stamp = timezone.localtime(bill.created_at).strftime("%Y%m%d-%H%M")
    name = "warana-receipt-%s-%s.pdf" % (bill.bill_number, stamp)
    response = HttpResponse(exports.receipt_pdf(bill), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="%s"' % name
    return response


@login_required
def bills(request):
    rows = Bill.objects.select_related("customer", "payment_info").annotate(
        units=Coalesce(Sum("items__quantity"), Value(0))
    )
    q = request.GET.get("q", "").strip()
    if q:
        rows = rows.filter(Q(bill_number__icontains=q) | Q(customer__name__icontains=q))
    return render(request, "bakery/bills.html", {"bills": rows[:200], "q": q})


@login_required
def bills_csv(request):
    """Download the bill history. Honours the same ?q= filter as the table so
    a search can be exported instead of the whole ledger."""
    rows = Bill.objects.select_related("customer", "cashier", "payment_info")
    q = request.GET.get("q", "").strip()
    if q:
        rows = rows.filter(
            Q(bill_number__icontains=q) | Q(customer__name__icontains=q)
        )
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.bills_csv(rows[:2000]), content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="warana-bills-%s.csv"' % stamp
    )
    return response


# ---------------------------------------------------------------- reports

def _parse_report_date(raw, default):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def _report_filters(request):
    """Read the Daily / Monthly / Yearly filter from the query string."""
    today = timezone.localdate()
    period = (request.GET.get("period") or "daily").lower()
    if period not in ("daily", "monthly", "yearly"):
        period = "daily"

    if period == "daily":
        start = _parse_report_date(request.GET.get("date"), today)
        end = start
        label = start.strftime("%d %b %Y")
        date_value = start.isoformat()
        month_value = start.strftime("%Y-%m")
        year_value = start.year
    elif period == "monthly":
        raw_month = (request.GET.get("month") or today.strftime("%Y-%m")).strip()
        try:
            year, month = [int(part) for part in raw_month.split("-")[:2]]
            if not 2000 <= year <= 2100:
                raise ValueError
            start = date(year, month, 1)
        except (TypeError, ValueError):
            start = today.replace(day=1)
        last_day = calendar.monthrange(start.year, start.month)[1]
        end = date(start.year, start.month, last_day)
        label = start.strftime("%B %Y")
        date_value = start.isoformat()
        month_value = start.strftime("%Y-%m")
        year_value = start.year
    else:
        raw_year = (request.GET.get("year") or str(today.year)).strip()
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            year = today.year
        if not 2000 <= year <= 2100:
            year = today.year
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        label = str(year)
        date_value = today.isoformat()
        month_value = "%d-01" % year
        year_value = year

    return {
        "period": period,
        "start": start,
        "end": end,
        "label": label,
        "date_value": date_value,
        "month_value": month_value,
        "year_value": year_value,
    }


def _bill_totals(qs):
    return qs.aggregate(
        bills=Count("id"),
        gross=Coalesce(Sum("subtotal"), Value(Decimal("0")), output_field=DecimalField()),
        discount=Coalesce(Sum("discount"), Value(Decimal("0")), output_field=DecimalField()),
        tax=Coalesce(Sum("tax_amount"), Value(Decimal("0")), output_field=DecimalField()),
        net=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField()),
    )


def _purchase_totals(qs):
    return qs.aggregate(
        purchases=Count("id"),
        gross=Coalesce(Sum("subtotal"), Value(Decimal("0")), output_field=DecimalField()),
        tax=Coalesce(Sum("tax_amount"), Value(Decimal("0")), output_field=DecimalField()),
        net=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField()),
    )


def _bill_group_rows(bill_qs, key):
    expression = TruncDate("created_at") if key == "day" else TruncMonth("created_at")
    grouped = (
        bill_qs.annotate(**{key: expression})
        .values(key)
        .annotate(
            bills=Count("id"),
            sales=Coalesce(Sum("subtotal"), Value(Decimal("0")), output_field=DecimalField()),
            tax=Coalesce(Sum("tax_amount"), Value(Decimal("0")), output_field=DecimalField()),
            revenue=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField()),
        )
    )
    rows = {}
    for row in grouped:
        group_key = row[key]
        if key == "month" and isinstance(group_key, datetime):
            group_key = group_key.date()
        rows[group_key] = row
    return rows


def _purchase_group_rows(purchase_qs, key):
    if key == "day":
        rows = (
            purchase_qs.values("purchase_date")
            .annotate(
                purchases=Count("id"),
                purchase_total=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField()),
                purchase_tax=Coalesce(Sum("tax_amount"), Value(Decimal("0")), output_field=DecimalField()),
            )
        )
        return {row["purchase_date"]: row for row in rows}

    rows = (
        purchase_qs.annotate(month=TruncMonth("purchase_date"))
        .values("month")
        .annotate(
            purchases=Count("id"),
            purchase_total=Coalesce(Sum("total"), Value(Decimal("0")), output_field=DecimalField()),
            purchase_tax=Coalesce(Sum("tax_amount"), Value(Decimal("0")), output_field=DecimalField()),
        )
    )
    return {row["month"]: row for row in rows}


def _report_context(request):
    filters = _report_filters(request)
    start, end = filters["start"], filters["end"]
    period = filters["period"]

    bill_qs = Bill.objects.filter(
        status="PAID", created_at__date__gte=start, created_at__date__lte=end
    )
    purchase_qs = Purchase.objects.filter(
        status="RECEIVED", purchase_date__gte=start, purchase_date__lte=end
    )

    sales = _bill_totals(bill_qs)
    purchases = _purchase_totals(purchase_qs)

    rows = []
    if period == "daily":
        rows.append({
            "label": start.strftime("%d %b %Y"),
            "bills": sales["bills"],
            "sales": sales["gross"],
            "tax": sales["tax"],
            "revenue": sales["net"],
            "purchases": purchases["purchases"],
            "purchase_total": purchases["net"],
            "purchase_tax": purchases["tax"],
        })
    elif period == "monthly":
        bill_by_day = _bill_group_rows(bill_qs, "day")
        purchase_by_day = _purchase_group_rows(purchase_qs, "day")
        last_day = calendar.monthrange(start.year, start.month)[1]
        for day_number in range(1, last_day + 1):
            day = date(start.year, start.month, day_number)
            b = bill_by_day.get(day, {})
            p = purchase_by_day.get(day, {})
            rows.append({
                "label": day.strftime("%d %b"),
                "bills": b.get("bills", 0),
                "sales": b.get("sales", Decimal("0")),
                "tax": b.get("tax", Decimal("0")),
                "revenue": b.get("revenue", Decimal("0")),
                "purchases": p.get("purchases", 0),
                "purchase_total": p.get("purchase_total", Decimal("0")),
                "purchase_tax": p.get("purchase_tax", Decimal("0")),
            })
    else:
        bill_by_month = _bill_group_rows(bill_qs, "month")
        purchase_by_month = _purchase_group_rows(purchase_qs, "month")
        for month_number in range(1, 13):
            month_start = date(start.year, month_number, 1)
            b = bill_by_month.get(month_start, {})
            p = purchase_by_month.get(month_start, {})
            rows.append({
                "label": month_start.strftime("%b %Y"),
                "bills": b.get("bills", 0),
                "sales": b.get("sales", Decimal("0")),
                "tax": b.get("tax", Decimal("0")),
                "revenue": b.get("revenue", Decimal("0")),
                "purchases": p.get("purchases", 0),
                "purchase_total": p.get("purchase_total", Decimal("0")),
                "purchase_tax": p.get("purchase_tax", Decimal("0")),
            })

    product_rows = (
        BillItem.objects.filter(bill__in=bill_qs)
        .values("product_name", "unit_price")
        .annotate(units=Sum("quantity"), revenue=Sum("line_total"), tax=Sum("tax_amount"))
        .order_by("-revenue")
    )
    payment_rows = list(
        Payment.objects.filter(bill__in=bill_qs)
        .values("method")
        .annotate(count=Count("id"), amount=Sum("amount"))
        .order_by("-amount")
    )
    best_sellers = (
        BillItem.objects.filter(bill__in=bill_qs)
        .values("product_name")
        .annotate(units=Sum("quantity"), revenue=Sum("line_total"))
        .order_by("-units")[:10]
    )

    context = dict(filters)
    context.update({
        "sales": sales,
        "purchases": purchases,
        "rows": rows,
        "product_rows": product_rows,
        "payment_rows": payment_rows,
        "best_sellers": best_sellers,
        "low_stock": Inventory.objects.select_related("product").filter(
            quantity__lte=LOW_STOCK_THRESHOLD
        ).order_by("quantity"),
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
        "stock_value": Inventory.objects.aggregate(
            v=Coalesce(Sum(F("quantity") * F("product__price")), Value(Decimal("0")),
                       output_field=DecimalField())
        )["v"],
    })
    return context


@login_required
def reports(request):
    return render(request, "bakery/reports.html", _report_context(request))


@login_required
def reports_export_csv(request):
    context = _report_context(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.report_csv(context), content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="warana-report-%s-%s.csv"' % (context["period"], stamp)
    )
    return response


@login_required
def reports_export_pdf(request):
    context = _report_context(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.report_pdf(context), content_type="application/pdf")
    response["Content-Disposition"] = (
        'attachment; filename="warana-report-%s-%s.pdf"' % (context["period"], stamp)
    )
    return response

# ---------------------------------------------------------------- report library

def _report_search(request):
    return (request.GET.get("q") or "").strip()


def _customer_report_rows(request):
    q = _report_search(request)
    rows = Customer.objects.annotate(
        bill_count=Count("bills", filter=Q(bills__status="PAID")),
        spend=Coalesce(
            Sum("bills__total", filter=Q(bills__status="PAID")),
            Value(Decimal("0")), output_field=DecimalField(),
        ),
        last_bill=Max("bills__created_at", filter=Q(bills__status="PAID")),
    )
    if q:
        rows = rows.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q)
        )
    return rows.order_by("-spend", "name"), q


@login_required
def report_customers(request):
    rows, q = _customer_report_rows(request)
    return render(request, "bakery/report_customers.html", {
        "rows": rows,
        "q": q,
        "total_customers": len(rows),
        "total_spend": sum((r.spend or Decimal("0") for r in rows), Decimal("0")),
    })


@login_required
def report_customers_csv(request):
    rows, _q = _customer_report_rows(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.customers_csv(rows), content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="warana-customers-%s.csv"' % stamp
    )
    return response


@login_required
def report_customers_pdf(request):
    rows, _q = _customer_report_rows(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    data = (
        (c.pk, c.name, c.phone, c.email or "-", c.bill_count,
         exports._pdf_money(c.spend), exports._local(c.last_bill) or "-")
        for c in rows
    )
    summary = "%s customers | Total spend %s" % (
        len(rows), exports._pdf_money(
            sum((r.spend or Decimal("0") for r in rows), Decimal("0"))
        ),
    )
    response = HttpResponse(exports.report_table_pdf(
        "Customer Report", "Paid bills only", exports.CUSTOMER_REPORT_FIELDS,
        data, summary, [0.55, 1.3, 1, 1.5, 0.5, 0.9, 1],
    ), content_type="application/pdf")
    response["Content-Disposition"] = (
        'attachment; filename="warana-customers-%s.pdf"' % stamp
    )
    return response


def _product_report_rows(request):
    q = _report_search(request)
    category = (request.GET.get("category") or "").strip()
    rows = Product.objects.select_related("category", "inventory").annotate(
        units_sold=Coalesce(
            Sum("billitem__quantity", filter=Q(billitem__bill__status="PAID")),
            Value(0),
        ),
        revenue=Coalesce(
            Sum("billitem__line_total", filter=Q(billitem__bill__status="PAID")),
            Value(Decimal("0")), output_field=DecimalField(),
        ),
    )
    if q:
        rows = rows.filter(
            Q(name__icontains=q) | Q(description__icontains=q)
            | Q(category__name__icontains=q)
        )
    if category:
        rows = rows.filter(category_id=category)
    return rows.order_by("category__name", "name"), q, category


@login_required
def report_products(request):
    rows, q, category = _product_report_rows(request)
    return render(request, "bakery/report_products.html", {
        "rows": rows,
        "q": q,
        "categories": Category.objects.all(),
        "selected_category": category,
        "total_products": len(rows),
        "total_units": sum((r.units_sold or 0 for r in rows), 0),
        "total_revenue": sum(
            (r.revenue or Decimal("0") for r in rows), Decimal("0")
        ),
    })


@login_required
def report_products_csv(request):
    rows, _q, _category = _product_report_rows(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.products_csv(rows), content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="warana-products-%s.csv"' % stamp
    )
    return response


@login_required
def report_products_pdf(request):
    rows, _q, _category = _product_report_rows(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    data = (
        (p.pk, p.name, p.category.name, p.stock,
         "%s / %s" % (exports._pdf_money(p.price), p.get_unit_display()),
         "%s%%" % p.tax_rate, p.units_sold, exports._pdf_money(p.revenue))
        for p in rows
    )
    summary = "%s products | %s units sold | Revenue %s" % (
        len(rows), sum((r.units_sold or 0 for r in rows), 0),
        exports._pdf_money(
            sum((r.revenue or Decimal("0") for r in rows), Decimal("0"))
        ),
    )
    response = HttpResponse(exports.report_table_pdf(
        "Product Report", "Product details and sales performance",
        exports.PRODUCT_REPORT_FIELDS, data, summary,
        [0.45, 1.3, 0.8, 0.5, 1, 0.5, 0.65, 0.8],
    ), content_type="application/pdf")
    response["Content-Disposition"] = (
        'attachment; filename="warana-products-%s.pdf"' % stamp
    )
    return response


def _stock_report_rows(request):
    q = _report_search(request)
    category = (request.GET.get("category") or "").strip()
    status = (request.GET.get("status") or "").strip().lower()
    rows = Inventory.objects.select_related("product", "product__category")
    if q:
        rows = rows.filter(
            Q(product__name__icontains=q) | Q(product__category__name__icontains=q)
        )
    if category:
        rows = rows.filter(product__category_id=category)
    if status == "low":
        rows = rows.filter(
            quantity__gt=0, quantity__lte=LOW_STOCK_THRESHOLD
        )
    elif status == "out":
        rows = rows.filter(quantity__lte=0)
    elif status == "ok":
        rows = rows.filter(quantity__gt=LOW_STOCK_THRESHOLD)
    else:
        status = ""
    return rows.order_by("quantity", "product__name"), q, category, status


@login_required
def report_stock(request):
    rows, q, category, status = _stock_report_rows(request)
    return render(request, "bakery/report_stock.html", {
        "rows": rows,
        "q": q,
        "categories": Category.objects.all(),
        "selected_category": category,
        "selected_status": status,
        "total_units": sum((r.quantity for r in rows), 0),
        "stock_value": sum(
            (r.stock_value for r in rows), Decimal("0")
        ),
        "low_count": sum(1 for r in rows if r.is_low),
        "out_count": sum(1 for r in rows if r.is_out),
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
    })


@login_required
def report_stock_csv(request):
    rows, _q, _category, _status = _stock_report_rows(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.stock_csv(rows), content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="warana-stock-%s.csv"' % stamp
    )
    return response


@login_required
def report_stock_pdf(request):
    rows, _q, _category, _status = _stock_report_rows(request)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    data = (
        (row.product.pk, row.product.name, row.product.category.name,
         row.quantity,
         "%s / %s" % (
             exports._pdf_money(row.product.price),
             row.product.get_unit_display(),
         ),
         exports._pdf_money(row.stock_value), row.stock_status_label)
        for row in rows
    )
    summary = "%s units | Stock value %s | Low stock %s | Out of stock %s" % (
        sum((r.quantity for r in rows), 0),
        exports._pdf_money(sum(
            (r.stock_value for r in rows), Decimal("0")
        )),
        sum(1 for r in rows if r.is_low),
        sum(1 for r in rows if r.is_out),
    )
    response = HttpResponse(exports.report_table_pdf(
        "Stock Report", "Current inventory position",
        exports.STOCK_REPORT_FIELDS, data, summary,
        [0.45, 1.3, 0.8, 0.5, 1, 0.9, 0.8],
    ), content_type="application/pdf")
    response["Content-Disposition"] = (
        'attachment; filename="warana-stock-%s.pdf"' % stamp
    )
    return response


def _bill_report_filters(request):
    today = timezone.localdate()
    start = _parse_report_date(request.GET.get("from"), today.replace(day=1))
    end = _parse_report_date(request.GET.get("to"), today)
    if start > end:
        start, end = end, start
    status = (request.GET.get("status") or "").strip().upper()
    if status not in ("PAID", "PENDING", "CANCELLED"):
        status = ""
    return {"start": start, "end": end, "status": status, "q": _report_search(request)}


def _bill_report_rows(filters):
    rows = (
        Bill.objects.select_related("customer", "payment_info")
        .filter(created_at__date__gte=filters["start"],
                created_at__date__lte=filters["end"])
        .annotate(units=Coalesce(Sum("items__quantity"), Value(0)))
    )
    if filters["status"]:
        rows = rows.filter(status=filters["status"])
    if filters["q"]:
        q = filters["q"]
        rows = rows.filter(
            Q(bill_number__icontains=q) | Q(customer__name__icontains=q)
            | Q(customer__phone__icontains=q)
        )
    return rows.order_by("-created_at", "-id")


@login_required
def report_bills(request):
    filters = _bill_report_filters(request)
    rows = _bill_report_rows(filters)
    return render(request, "bakery/report_bills.html", {
        "rows": rows,
        "filters": filters,
        "totals": _bill_totals(rows),
    })


@login_required
def report_bills_csv(request):
    filters = _bill_report_filters(request)
    rows = _bill_report_rows(filters)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(exports.bill_report_csv(rows), content_type="text/csv")
    response["Content-Disposition"] = (
        'attachment; filename="warana-bills-report-%s.csv"' % stamp
    )
    return response


@login_required
def report_bills_pdf(request):
    filters = _bill_report_filters(request)
    rows = _bill_report_rows(filters)
    totals = _bill_totals(rows)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    data = (
        (b.bill_number, exports._local(b.created_at),
         (b.customer.name if b.customer else "") or "Walk-in",
         (b.customer.phone if b.customer else "") or "-",
         b.cashier.username if b.cashier else "-", b.units,
         exports._pdf_money(b.subtotal), exports._pdf_money(b.discount),
         exports._pdf_money(b.tax_amount), exports._pdf_money(b.total),
         b.payment.get_method_display() if b.payment else "-",
         b.get_status_display())
        for b in rows
    )
    summary = "%s bills | Revenue %s | Date range %s to %s" % (
        totals["bills"], exports._pdf_money(totals["net"]),
        filters["start"].strftime("%d-%m-%Y"),
        filters["end"].strftime("%d-%m-%Y"),
    )
    response = HttpResponse(exports.report_table_pdf(
        "Bill Report", "Bills matching the selected filters",
        exports.BILLING_FIELDS, data, summary,
        [1.05, 0.95, 1.15, 0.9, 0.9, 0.4, 0.85, 0.8, 0.75, 0.85, 0.7, 0.75],
        landscape_page=True,
    ), content_type="application/pdf")
    response["Content-Disposition"] = (
        'attachment; filename="warana-bills-report-%s.pdf"' % stamp
    )
    return response
