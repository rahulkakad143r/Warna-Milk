import csv
import io
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from PIL import Image

from django.contrib.staticfiles import finders as staticfiles_finders
from django.contrib.auth.models import User
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import billing, exports, purchasing
from .forms import CustomerForm, ProductForm, PurchaseItemFormSet, PurchaseForm, SupplierForm
from .models import (Bill, Cart, CartItem, Category, Inventory, Payment,
                     Product, Purchase, PurchaseItem, Supplier)


def make_product(name, price, stock, reorder=5):
    category, _ = Category.objects.get_or_create(name="TestCat")
    product = Product.objects.create(
        name=name, category=category, price=Decimal(price), unit="pcs"
    )
    Inventory.objects.create(product=product, quantity=stock, reorder_level=reorder)
    return product


def pdf_text(blob):
    """Pull the drawn strings back out of a reportlab PDF.

    pypdf is not a project dependency, and installing one just to assert on a
    file we generate ourselves would be overkill, so the content streams are
    decoded here. reportlab wraps them in ASCII85 and then Flate.
    """
    import base64
    import zlib

    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\n?endstream", blob, re.S):
        raw = match.group(1).strip()
        # reportlab emits ASCII85 without the leading <~ marker
        wrapped = raw if raw.startswith(b"<~") else b"<~" + raw
        if not wrapped.endswith(b"~>"):
            wrapped += b"~>"
        try:
            raw = zlib.decompress(base64.a85decode(wrapped, adobe=True))
        except Exception:
            continue
        for token in re.findall(rb"\((?:\\.|[^()\\])*\)", raw):
            text = re.sub(rb"\\([()\\])", rb"\1", token[1:-1])
            chunks.append(text.decode("latin-1"))
    return re.sub(r"\s+", " ", " ".join(chunks))


class ProductImageTests(TestCase):
    """A product photo has to survive the round trip. Both halves are easy to
    break silently: without enctype on the form the browser never sends the
    file, and without request.FILES in the view the upload is dropped on the
    floor - the field just quietly stays empty."""

    def setUp(self):
        self.user = User.objects.create_user("shopadmin", password="x")
        self.client.force_login(self.user)
        self.cat = Category.objects.create(name="PhotoCat")
        self.tmp = tempfile.mkdtemp(prefix="warana-media-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        media = override_settings(MEDIA_ROOT=self.tmp)
        media.enable()
        self.addCleanup(media.disable)

    def jpeg(self, name="croissant.jpg"):
        import io
        buf = io.BytesIO()
        Image.new("RGB", (240, 180), (198, 140, 70)).save(buf, "JPEG")
        return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")

    def test_upload_form_can_actually_send_a_file(self):
        """No enctype means the browser drops the file before it leaves."""
        template = (Path(settings.BASE_DIR) / "bakery" / "templates" / "bakery"
                    / "product_form.html").read_text(encoding="utf-8")
        self.assertIn('enctype="multipart/form-data"', template)

    def test_product_create_saves_an_uploaded_photo(self):
        r = self.client.post(reverse("bakery:product_create"), {
            "name": "Butter Croissant", "category": self.cat.pk,
            "price": "40", "unit": "pcs", "image": self.jpeg(),
            "description": "", "is_active": "on",
        })
        self.assertEqual(r.status_code, 302)
        p = Product.objects.get(name="Butter Croissant")
        self.assertTrue(p.image, "uploaded photo was silently discarded")
        self.assertTrue(Path(p.image.path).exists(), "photo never hit disk")
        self.assertIn(p.image.url, self.client.get(reverse("bakery:products")).content.decode())

    def test_product_edit_saves_an_uploaded_photo(self):
        p = Product.objects.create(name="Veg Puff", category=self.cat,
                                   price=Decimal("25"), unit="pcs")
        r = self.client.post(reverse("bakery:product_edit", args=[p.pk]), {
            "name": "Veg Puff", "category": self.cat.pk, "price": "25",
            "unit": "pcs", "image": self.jpeg("puff.jpg"),
            "description": "", "is_active": "on",
        })
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertTrue(p.image, "edit upload was silently discarded")

    def test_uploaded_photo_wins_over_a_remote_url(self):
        p = Product.objects.create(name="Milk Bread", category=self.cat,
                                   price=Decimal("30"), unit="pcs",
                                   image_url="https://example.com/bread.jpg")
        html = self.client.get(reverse("bakery:products")).content.decode()
        self.assertIn("https://example.com/bread.jpg", html,
                      "a product with only a URL must still show a photo")
        p.image = self.jpeg("bread.jpg")
        p.save()
        html = self.client.get(reverse("bakery:products")).content.decode()
        self.assertIn(p.image.url, html)
        self.assertNotIn("https://example.com/bread.jpg", html,
                         "an uploaded photo must take priority over the URL")

    def test_no_photo_falls_back_to_a_tiled_placeholder(self):
        make_product("Plain Cake", "100", 4)
        html = self.client.get(reverse("bakery:products")).content.decode()
        self.assertNotIn("<img", html.split("name-cell")[1].split("</td>")[0],
                         "a product with no photo must not render a broken img")


class BillingExportTests(TestCase):
    """Billing must offer a download and a print path, not just an on-screen
    receipt: a PDF for a single bill and a CSV for the ledger."""

    def setUp(self):
        self.user = User.objects.create_user("exporter", password="x")
        self.client.force_login(self.user)
        bread = make_product("Milk Bread", "60.00", 10)
        cake = make_product("Vanilla Cup Cake", "55.00", 4, reorder=2)
        cart = Cart.objects.create(session_key="export-session")
        CartItem.objects.create(cart=cart, product=bread, quantity=2)
        CartItem.objects.create(cart=cart, product=cake, quantity=1)
        self.bill = billing.checkout(cart, cashier=self.user, method="UPI",
                                     reference="UPI-7781")

    # ---------------------------------------------------------------- PDF

    def test_receipt_pdf_downloads_as_a_real_pdf(self):
        r = self.client.get(reverse("bakery:receipt_pdf", args=[self.bill.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn(self.bill.bill_number, r["Content-Disposition"])
        self.assertTrue(r.content.startswith(b"%PDF-"), "not a PDF payload")
        self.assertIn(b"%%EOF", r.content[-1024:], "PDF was truncated")
        self.assertIn(b"/Subtype /Image", r.content,
                      "the Warana Milk logo is missing from the PDF letterhead")

    def test_receipt_pdf_carries_the_bill_details(self):
        r = self.client.get(reverse("bakery:receipt_pdf", args=[self.bill.pk]))
        text = pdf_text(r.content)

        self.assertIn(self.bill.bill_number, text)
        self.assertIn("Warana", text)
        self.assertNotIn("Express", text)
        self.assertNotIn("Varna", text)
        self.assertIn("Milk Bread", text)
        self.assertIn("Vanilla Cup Cake", text)
        self.assertIn("Total Paid", text)
        self.assertIn("UPI", text)
        self.assertIn("UPI-7781", text)
        # the rupee sign has no slot in Helvetica's WinAnsi encoding, so the
        # PDF must spell it out rather than emit a broken glyph
        self.assertIn("Rs.", text)
        self.assertNotIn("\u20b9", text)
        self.assertNotIn("(n)", text)
        # the rate has to read the same as the on-screen receipt (floatformat:0)
        self.assertIn("Tax (5%)", text)
        # the arithmetic printed has to match the stored bill
        squeezed = text.replace(" ", "")
        for field in ("subtotal", "tax_amount", "total"):
            self.assertIn("%.2f" % float(getattr(self.bill, field)), squeezed,
                          "%s missing from the PDF" % field)

    def test_receipt_pdf_is_one_page_for_a_normal_bill(self):
        r = self.client.get(reverse("bakery:receipt_pdf", args=[self.bill.pk]))
        pages = len(re.findall(rb"/Type\s*/Page[^s]", r.content))
        self.assertEqual(pages, 1)

    def test_receipt_pdf_404s_for_an_unknown_bill(self):
        self.assertEqual(
            self.client.get(reverse("bakery:receipt_pdf", args=[99999])).status_code,
            404,
        )

    # ---------------------------------------------------------------- CSV

    def test_bills_csv_downloads_with_a_header_and_every_bill(self):
        r = self.client.get(reverse("bakery:bills_csv"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn(".csv", r["Content-Disposition"])

        text = r.content.decode("utf-8-sig")  # utf-8-sig strips the Excel BOM
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], exports.BILLING_FIELDS)
        self.assertEqual(len(rows), 2)  # header + the one bill
        self.assertEqual(rows[1][0], self.bill.bill_number)
        self.assertIn("UPI", rows[1])
        self.assertIn("Paid", rows[1][-1])
        self.assertEqual(rows[1][exports.BILLING_FIELDS.index("Customer")],
                         "Walk-in")
        self.assertEqual(rows[1][exports.BILLING_FIELDS.index("Cashier")],
                         "exporter")
        # item count and money have to survive the export
        self.assertIn("3", rows[1][exports.BILLING_FIELDS.index("Items")])
        self.assertIn("%.2f" % float(self.bill.total),
                      rows[1][exports.BILLING_FIELDS.index("Total")])

    def test_bills_csv_has_a_bom_so_excel_reads_it(self):
        r = self.client.get(reverse("bakery:bills_csv"))
        self.assertTrue(r.content.startswith(b"\xef\xbb\xbf"))

    def test_reports_page_and_exports_work_for_each_period(self):
        for period in ("daily", "monthly", "yearly"):
            with self.subTest(period=period):
                page = self.client.get(reverse("bakery:reports"), {"period": period})
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, "Period Breakdown")

                csv_response = self.client.get(
                    reverse("bakery:reports_csv"), {"period": period}
                )
                self.assertEqual(csv_response.status_code, 200)
                self.assertIn("text/csv", csv_response["Content-Type"])
                self.assertTrue(csv_response.content.startswith(b"\xef\xbb\xbf"))

                pdf_response = self.client.get(
                    reverse("bakery:reports_pdf"), {"period": period}
                )
                self.assertEqual(pdf_response.status_code, 200)
                self.assertEqual(pdf_response["Content-Type"], "application/pdf")
                self.assertTrue(pdf_response.content.startswith(b"%PDF-"))
                self.assertIn(b"/Subtype /Image", pdf_response.content)
                self.assertIn("WARANA MILK", pdf_text(pdf_response.content))

    def test_daily_monthly_and_yearly_reports_include_database_totals(self):
        from django.utils import timezone

        today = timezone.localdate()
        supplier = Supplier.default_supplier()
        product = self.bill.items.first().product
        purchasing.record_purchase(
            reference="REPORT-INV-1",
            supplier=supplier,
            purchase_date=today,
            lines=[{
                "product": product,
                "quantity": 1,
                "unit_cost": Decimal("100.00"),
                "tax_rate": Decimal("12.00"),
            }],
        )

        filters = (
            ("daily", {"date": today.isoformat()}, today.strftime("%d %b %Y")),
            ("monthly", {"month": today.strftime("%Y-%m")}, today.strftime("%d %b")),
            ("yearly", {"year": str(today.year)}, today.strftime("%b %Y")),
        )
        for period, params, row_label in filters:
            with self.subTest(period=period):
                response = self.client.get(
                    reverse("bakery:reports"), {"period": period, **params}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["sales"]["bills"], 1)
                self.assertEqual(response.context["sales"]["gross"], Decimal("175.00"))
                self.assertEqual(response.context["sales"]["tax"], Decimal("8.75"))
                self.assertEqual(response.context["sales"]["net"], Decimal("183.75"))
                self.assertEqual(response.context["purchases"]["purchases"], 1)
                self.assertEqual(response.context["purchases"]["net"], Decimal("112.00"))

                row = next(r for r in response.context["rows"] if r["label"] == row_label)
                self.assertEqual(row["sales"], Decimal("175.00"))
                self.assertEqual(row["tax"], Decimal("8.75"))
                self.assertEqual(row["revenue"], Decimal("183.75"))
                self.assertEqual(row["purchase_total"], Decimal("112.00"))

    def test_yearly_report_rejects_out_of_range_year_without_error(self):
        from django.utils import timezone

        response = self.client.get(
            reverse("bakery:reports"), {"period": "yearly", "year": "999999"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["year_value"], timezone.localdate().year)

    def test_bills_csv_respects_the_same_search_as_the_table(self):
        other = Cart.objects.create(session_key="other")
        CartItem.objects.create(cart=other, product=make_product("Brownie", "85.00", 5),
                                quantity=1)
        billing.checkout(other, cashier=self.user)
        self.assertEqual(Bill.objects.count(), 2)

        hit = self.client.get(reverse("bakery:bills_csv"),
                              {"q": self.bill.bill_number})
        rows = list(csv.reader(io.StringIO(
            hit.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], self.bill.bill_number)

        miss = self.client.get(reverse("bakery:bills_csv"), {"q": "no-such-bill"})
        self.assertEqual(len(list(csv.reader(io.StringIO(
            miss.content.decode("utf-8-sig"))))), 1)  # header only

    # -------------------------------------------------------------- links

    def test_billing_pages_expose_download_and_print_controls(self):
        bills = self.client.get(reverse("bakery:bills")).content.decode()
        self.assertIn(reverse("bakery:bills_csv"), bills)
        self.assertIn("window.print()", bills)
        # each ledger row can go straight to its own PDF
        self.assertIn(reverse("bakery:receipt_pdf", args=[self.bill.pk]), bills)

        receipt = self.client.get(
            reverse("bakery:receipt", args=[self.bill.pk])).content.decode()
        self.assertIn(reverse("bakery:receipt_pdf", args=[self.bill.pk]), receipt)
        self.assertIn("window.print()", receipt)

    def test_exports_require_login(self):
        self.client.logout()
        for name, args in (("bakery:receipt_pdf", [self.bill.pk]),
                           ("bakery:bills_csv", [])):
            r = self.client.get(reverse(name, args=args))
            self.assertIn(r.status_code, (302, 403), name)
            self.assertNotIn(getattr(r, "content", b""), (b"%PDF",))


class BillingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("cashier1", password="x")
        self.client.force_login(self.user)
        self.p1 = make_product("Bread", "50.00", 10)
        self.p2 = make_product("Cake", "100.00", 3, reorder=2)

    def cart_with(self, *pairs):
        self._cart_seq = getattr(self, "_cart_seq", 0) + 1
        cart = Cart.objects.create(session_key=f"test-session-{self._cart_seq}")
        for product, qty in pairs:
            CartItem.objects.create(cart=cart, product=product, quantity=qty)
        return cart

    def test_totals_include_discount_and_tax(self):
        cart = self.cart_with((self.p1, 2), (self.p2, 1))
        subtotal, discount, tax, total = billing.compute_totals(
            list(cart.items), Decimal("50"), Decimal("10")
        )
        self.assertEqual(subtotal, Decimal("200.00"))
        self.assertEqual(discount, Decimal("50.00"))
        self.assertEqual(tax, Decimal("15.00"))
        self.assertEqual(total, Decimal("165.00"))

    def test_checkout_uses_each_product_own_gst_rate(self):
        self.p1.tax_rate = Decimal("5.00")
        self.p1.save(update_fields=["tax_rate"])
        self.p2.tax_rate = Decimal("12.00")
        self.p2.save(update_fields=["tax_rate"])
        cart = self.cart_with((self.p1, 2), (self.p2, 1))

        bill = billing.checkout(cart, cashier=self.user, method="CASH")
        bread = bill.items.get(product=self.p1)
        cake = bill.items.get(product=self.p2)

        self.assertEqual(bread.tax_rate, Decimal("5.00"))
        self.assertEqual(bread.tax_amount, Decimal("5.00"))
        self.assertEqual(cake.tax_rate, Decimal("12.00"))
        self.assertEqual(cake.tax_amount, Decimal("12.00"))
        self.assertEqual(bill.tax_amount, Decimal("17.00"))
        self.assertEqual(bill.total, Decimal("217.00"))

    def test_checkout_applies_product_discounts_before_gst(self):
        self.p1.discount_percent = Decimal("10.00")
        self.p1.save(update_fields=["discount_percent"])
        self.p2.discount_percent = Decimal("20.00")
        self.p2.save(update_fields=["discount_percent"])
        cart = self.cart_with((self.p1, 2), (self.p2, 1))

        bill = billing.checkout(cart, cashier=self.user, method="CASH")

        self.assertEqual(bill.subtotal, Decimal("200.00"))
        self.assertEqual(bill.discount, Decimal("30.00"))
        self.assertEqual(bill.items.get(product=self.p1).tax_amount, Decimal("4.50"))
        self.assertEqual(bill.items.get(product=self.p2).tax_amount, Decimal("4.00"))
        self.assertEqual(bill.tax_amount, Decimal("8.50"))
        self.assertEqual(bill.total, Decimal("178.50"))

    def test_product_wise_gst_matches_mixed_rate_example(self):
        category = Category.objects.get(name="TestCat")
        products = []
        for name, price, rate in (
            ("GST Product A", "100.00", "5.00"),
            ("GST Product B", "200.00", "12.00"),
            ("GST Product C", "150.00", "18.00"),
        ):
            product = Product.objects.create(
                name=name, category=category, price=Decimal(price),
                unit="pcs", tax_rate=Decimal(rate),
            )
            Inventory.objects.create(product=product, quantity=2)
            products.append(product)

        cart = self.cart_with(*((product, 1) for product in products))
        bill = billing.checkout(cart, cashier=self.user)
        self.assertEqual(
            [item.tax_amount for item in bill.items.order_by("product_name")],
            [Decimal("5.00"), Decimal("24.00"), Decimal("27.00")],
        )
        self.assertEqual(bill.tax_amount, Decimal("56.00"))
        self.assertEqual(bill.total, Decimal("506.00"))

    def test_checkout_creates_bill_and_deducts_stock(self):
        cart = self.cart_with((self.p1, 2), (self.p2, 1))
        bill = billing.checkout(cart, cashier=self.user, method="CASH")

        self.assertTrue(bill.bill_number.startswith("GKB-"))
        self.assertEqual(bill.item_count, 3)
        self.assertEqual(bill.status, "PAID")
        self.assertEqual(bill.total, Decimal("210.00"))  # 200 subtotal + 5% tax

        self.assertEqual(Inventory.objects.get(product=self.p1).quantity, 8)
        self.assertEqual(Inventory.objects.get(product=self.p2).quantity, 2)

        self.assertTrue(Payment.objects.filter(bill=bill, method="CASH").exists())
        self.assertEqual(cart.cart_items.count(), 0)

    def test_bill_numbers_increment_within_a_day(self):
        first = billing.checkout(self.cart_with((self.p1, 1)), cashier=self.user)
        second = billing.checkout(self.cart_with((self.p1, 1)), cashier=self.user)
        self.assertLess(first.bill_number, second.bill_number)

    def test_bill_numbers_stay_unique_when_a_gap_exists(self):
        from django.utils import timezone
        first = billing.checkout(self.cart_with((self.p1, 1)), cashier=self.user)
        second = billing.checkout(self.cart_with((self.p1, 1)), cashier=self.user)
        second.delete()  # leaves a gap in the daily sequence

        third = billing.checkout(self.cart_with((self.p1, 1)), cashier=self.user)
        prefix = f"GKB-{timezone.localdate():%Y%m%d}-"

        self.assertEqual(first.bill_number, f"{prefix}0001")
        self.assertEqual(third.bill_number, f"{prefix}0002")
        self.assertEqual(
            Bill.objects.values("bill_number").distinct().count(),
            Bill.objects.count(),
        )

    def test_insufficient_stock_is_rejected(self):
        cart = self.cart_with((self.p2, 5))
        with self.assertRaises(ValueError):
            billing.checkout(cart, cashier=self.user)
        self.assertEqual(Inventory.objects.get(product=self.p2).quantity, 3)
        self.assertEqual(Bill.objects.count(), 0)

    def test_empty_cart_is_rejected(self):
        with self.assertRaises(ValueError):
            billing.checkout(Cart.objects.create(session_key="empty"), cashier=self.user)


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", password="pw")
        self.p1 = make_product("Bread", "50.00", 10)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("bakery:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('login')}?next=/")

    def test_login_then_reach_dashboard(self):
        response = self.client.post(
            reverse("login"), {"username": "u", "password": "pw"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [("/", 302)])
        self.assertContains(response, "Dashboard")
        self.assertContains(response, "Low Stock Alerts")
        self.assertContains(response, "Low Stock! Only 10 units remaining.")
        self.assertTrue(response.context["user"].is_authenticated)

    def test_pages_render(self):
        self.client.force_login(self.user)
        for name in ["bakery:dashboard", "bakery:products", "bakery:inventory",
                     "bakery:browse", "bakery:cart", "bakery:bills", "bakery:reports"]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_browse_and_cart_pages_share_one_live_cart(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 2}
        )
        cart_page = self.client.get(reverse("bakery:cart"))
        browse_page = self.client.get(reverse("bakery:browse"))
        self.assertContains(cart_page, 'value="2"')
        self.assertContains(browse_page, "Current Cart")
        self.assertContains(browse_page, 'value="2"')

        item = CartItem.objects.get(product=self.p1)
        self.client.post(
            reverse("bakery:cart_update", args=[item.pk]),
            {"quantity": "3", "next": reverse("bakery:browse")},
        )
        self.assertContains(self.client.get(reverse("bakery:cart")), 'value="3"')
        self.assertContains(self.client.get(reverse("bakery:browse")), 'value="3"')
        self.assertEqual(Cart.objects.count(), 1)

        self.client.post(
            reverse("bakery:cart_remove", args=[item.pk]),
            {"next": reverse("bakery:browse")},
        )
        self.assertContains(self.client.get(reverse("bakery:cart")), "Your cart is empty.")
        self.assertContains(self.client.get(reverse("bakery:browse")), "Your cart is empty.")
        self.assertEqual(Cart.objects.count(), 1)

    def test_checkout_displays_and_saves_automatic_product_discount(self):
        self.client.force_login(self.user)
        self.p1.discount_percent = Decimal("10.00")
        self.p1.save(update_fields=["discount_percent"])
        self.client.post(
            reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 1}
        )

        checkout_page = self.client.get(reverse("bakery:checkout"))
        self.assertEqual(checkout_page.context["discount"], Decimal("5.00"))
        self.assertContains(checkout_page, "10.00% off")

        response = self.client.post(reverse("bakery:checkout"), {
            "method": "CASH", "discount": "999",
        })

        self.assertEqual(response.status_code, 302)
        bill = Bill.objects.get()
        self.assertEqual(bill.discount, Decimal("5.00"))
        self.assertEqual(bill.total, Decimal("47.25"))

    def test_browse_category_filter_shows_correct_products(self):
        self.client.force_login(self.user)
        cat = Category.objects.create(name="Snacks", tax_rate=Decimal("12.00"))
        other = Product.objects.create(
            name="Chips", category=cat, price=Decimal("20"), unit="pack"
        )
        # the select lives inside a GET form, so onchange can submit it
        response = self.client.get(reverse("bakery:browse"))
        self.assertContains(response, '<form method="get" action="%s"' % reverse("bakery:browse"))
        self.assertContains(response, f'name="category"')
        self.assertContains(response, "All categories")

        response = self.client.get(reverse("bakery:browse"), {"category": cat.pk})
        self.assertContains(response, "Chips")
        self.assertNotContains(response, self.p1.name)  # "Bread" is not in Snacks

        response = self.client.get(reverse("bakery:browse"))
        self.assertContains(response, self.p1.name)

    def test_browse_add_cart_and_checkout_via_views(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 2})
        self.assertEqual(CartItem.objects.count(), 1)

        response = self.client.post(reverse("bakery:checkout"), {
            "customer_name": "Ravi", "customer_phone": "9800000001",
            "discount": "0", "tax_rate": "5", "method": "UPI", "reference": "ravi@upi",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Bill.objects.count(), 1)
        self.assertEqual(Inventory.objects.get(product=self.p1).quantity, 8)
        self.assertEqual(CartItem.objects.count(), 0)

    def test_cart_quantity_capped_at_available_stock(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 4})
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 40})
        self.assertEqual(CartItem.objects.get().quantity, 10)

    def test_add_cannot_create_zero_or_negative_quantity(self):
        self.client.force_login(self.user)
        for attempt, bad in enumerate(("0", "-5", "not-a-number", ""), start=1):
            with self.subTest(quantity=bad):
                self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": bad})
                # junk clamps to 1, so the line only ever grows by 1 and is never 0
                self.assertEqual(CartItem.objects.get().quantity, attempt)

    def test_out_of_stock_product_cannot_be_added(self):
        empty = make_product("Sold Out Cake", "99.00", 0)
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[empty.pk]), {"quantity": 1})
        self.assertFalse(CartItem.objects.filter(product=empty).exists())

    def test_browse_disables_add_button_for_out_of_stock(self):
        make_product("Sold Out Cake", "99.00", 0)
        self.client.force_login(self.user)
        response = self.client.get(reverse("bakery:browse"))
        self.assertContains(response,
                            '<button class="btn btn-primary grow" type="submit" disabled>'
                            "Add to Cart</button>",
                            count=1)

    def test_cart_update_cannot_set_zero_quantity(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 3})
        item = CartItem.objects.get()
        # asking for 0 deletes the line, it must never persist as 0
        self.client.post(reverse("bakery:cart_update", args=[item.pk]), {"quantity": 0})
        self.assertFalse(CartItem.objects.exists())

    def test_cart_update_removes_products_that_became_out_of_stock(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 1})
        item = CartItem.objects.get()
        Inventory.objects.filter(product=self.p1).update(quantity=0)

        response = self.client.post(
            reverse("bakery:cart_update", args=[item.pk]), {"quantity": 1}, follow=True
        )
        self.assertFalse(CartItem.objects.exists())
        self.assertContains(response, "out of stock and was removed from the cart")

    def test_cart_update_rejects_junk_without_error(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 3})
        item = CartItem.objects.get()
        response = self.client.post(
            reverse("bakery:cart_update", args=[item.pk]), {"quantity": "abc"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CartItem.objects.get().quantity, 3)

    def test_oversized_cart_is_clamped_then_bills_real_stock(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 999})
        self.assertEqual(CartItem.objects.get().quantity, 10)

        response = self.client.post(reverse("bakery:checkout"), {
            "customer_name": "", "customer_phone": "",
            "discount": "0", "tax_rate": "5", "method": "CASH", "reference": "",
        })
        self.assertEqual(response.status_code, 302)
        bill = Bill.objects.get()
        self.assertEqual(bill.items.get().quantity, 10)
        self.assertEqual(Inventory.objects.get(product=self.p1).quantity, 0)

    def test_product_name_with_quote_is_escaped_in_confirm(self):
        make_product("Ann's Bread", "10.00", 5)
        self.client.force_login(self.user)
        html = self.client.get(reverse("bakery:products")).content.decode()
        # the raw apostrophe must not survive into the inline confirm() JS string
        self.assertIn(r"Ann\u0027s Bread", html)
        self.assertNotIn("confirm('Delete Ann's Bread?')", html)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get("/login/").status_code, 200)

    def test_receipt_survives_a_deleted_cashier(self):
        """Bill.cashier is SET_NULL, so old bills can have no cashier at all."""
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.p1.pk]), {"quantity": 1})
        self.client.post(reverse("bakery:checkout"), {
            "customer_name": "Ravi", "customer_phone": "9800000001",
            "discount": "0", "tax_rate": "5", "method": "CASH", "reference": "",
        })
        bill = Bill.objects.get()

        User.objects.filter(pk=self.user.pk).delete()  # cashier gone -> SET_NULL
        bill.refresh_from_db()
        self.assertIsNone(bill.cashier)

        self.client.force_login(User.objects.create_superuser(
            "boss", "boss@waranamilk.local", "x"))
        response = self.client.get(reverse("bakery:receipt", args=[bill.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, bill.bill_number)

    def test_logout_button_logs_out_and_lands_on_login(self):
        """The sidebar control is a POST form; Django 5 answers GET with 405."""
        self.client.force_login(self.user)

        html = self.client.get(reverse("bakery:dashboard")).content.decode()
        self.assertIn('action="/logout/"', html)
        self.assertIn('method="post"', html)

        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.headers["Location"])

        # session is really gone: protected pages bounce back to login
        bounce = self.client.get(reverse("bakery:dashboard"))
        self.assertEqual(bounce.status_code, 302)
        self.assertTrue(
            bounce.url.startswith(reverse("login")),
            f"expected a redirect to login, got {bounce.url}",
        )
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_is_the_untouched_entry_point(self):
        """Anyone hitting a protected page while logged out is sent to login."""
        for url in (reverse("bakery:dashboard"), reverse("bakery:products"),
                    reverse("bakery:bills"), reverse("bakery:reports")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response.headers["Location"])
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_logo_renders_on_every_branded_page(self):
        """Catches a missing {% load static %} in a child template, which
        would otherwise blow up only on that one page."""
        self.client.force_login(self.user)
        bill = Bill.objects.create(
            bill_number="VRN-20260925-0001", subtotal=Decimal("50"),
            tax_amount=Decimal("2.50"), total=Decimal("52.50"), status="PAID",
        )
        urls = [reverse("bakery:dashboard"), reverse("bakery:browse"),
                reverse("bakery:products"), reverse("bakery:inventory"),
                reverse("bakery:cart"), reverse("bakery:checkout"),
                reverse("bakery:bills"), reverse("bakery:reports"),
                reverse("bakery:receipt", args=[bill.pk])]
        for url in urls:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn("/static/bakery/img/logo.png", html)
                self.assertIn('alt="Warana Milk"', html)

    def test_page_band_shows_on_every_signed_in_page_only(self):
        """The decorative band belongs to the shared post-login shell, so it
        must reach every app page and must never leak onto the login page."""
        self.client.force_login(self.user)
        bill = Bill.objects.create(
            bill_number="VRN-20260925-0002", subtotal=Decimal("50"),
            tax_amount=Decimal("2.50"), total=Decimal("52.50"), status="PAID",
        )
        urls = [reverse("bakery:dashboard"), reverse("bakery:browse"),
                reverse("bakery:products"), reverse("bakery:inventory"),
                reverse("bakery:cart"), reverse("bakery:checkout"),
                reverse("bakery:bills"), reverse("bakery:reports"),
                reverse("bakery:receipt", args=[bill.pk])]
        for url in urls:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn("/static/bakery/img/page-band.png?v=2", html)
                self.assertIn('class="page-band"', html)

        # the asset itself must exist, not 404 as a broken band
        self.assertIsNotNone(
            staticfiles_finders.find("bakery/img/page-band.png"))

        # login keeps its own hero and stays band-free
        login_html = self.client.get("/login/").content.decode()
        self.assertNotIn("page-band", login_html)

        # and it must stay out of printed receipts
        css = Path(staticfiles_finders.find("bakery/css/style.css")).read_text(
            encoding="utf-8")
        print_rule = css.split("@media print", 1)[1]
        self.assertIn(".page-band", print_rule)

    def test_login_page_uses_the_logo_and_a_real_post_form(self):
        """The page shows the real Warana logo and must POST to Django
        rather than fake it with alert()."""
        html = self.client.get("/login/").content.decode()
        self.assertIn("Warana", html)
        self.assertNotIn("Express", html)
        self.assertIn("Bakery Inventory &amp; Billing System", html)
        self.assertIn('id="loginForm"', html)
        self.assertIn('method="post"', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn("login.css", html)
        # the uploaded logo, not a text wordmark
        self.assertIn("bakery/img/logo.png?v=2", html)
        self.assertIn(">Warana</h1>", html)
        # the hero photo must exist, not 404 behind the overlay
        css = Path(staticfiles_finders.find("bakery/css/login.css")).read_text(
            encoding="utf-8")
        self.assertIn("../img/bakery-bg.jpg", css)
        self.assertIsNotNone(staticfiles_finders.find("bakery/img/bakery-bg.jpg"))
        # the right panel is deliberately plain - no corner artwork
        self.assertNotIn("login-panel.jpg", css)
        self.assertIsNone(staticfiles_finders.find("bakery/img/login-panel.jpg"))
        # the photo is bright and the hero text is white, so the end of the
        # overlay ramp that sits behind the copy has to stay heavy. The far
        # end may stay light because no text reaches it.
        ramp = re.findall(r"rgba\(45,\s*20,\s*8,\s*([\d.]+)\)", css)
        self.assertTrue(ramp, "hero overlay gradient not found")
        self.assertGreaterEqual(
            float(ramp[0]), 0.6,
            "overlay behind the hero copy is too light for white text")
        hero = css[css.index(".hero-content {"):css.index("}", css.index(".hero-content {"))]
        self.assertIn("max-width", hero,
                      "hero copy must stay width-bounded, clear of the light end")
        # the fake frontend login must not come back
        self.assertNotIn("alert(\"Login Successful!", html)
        self.assertNotIn("function login()", html)

    def test_sidebar_fits_without_scrolling_on_short_screens(self):
        """The fixed sidebar must not need its own scrollbar on a normal
        laptop. Its content is a fixed ~809px tall, so on a 1366x768 or
        1280x720 screen hard-coded padding used to clip the logout button.
        The vertical rhythm therefore scales with viewport height, and
        overflow-y stays only as a fallback for very short windows."""
        css = Path(staticfiles_finders.find("bakery/css/style.css")).read_text(
            encoding="utf-8")

        start = css.index(".sidebar {")
        block = css[start:css.index("}", start)]
        # a full-height fixed column: height comes from the viewport, not content
        self.assertRegex(block, r"position:\s*fixed")
        self.assertRegex(block, r"inset:\s*0 auto 0 0")
        # padding and the gaps between brand / nav / cart / user must be
        # viewport-relative, otherwise the column outgrows a 768px screen
        for prop in ("padding", "gap"):
            value = re.search(prop + r":\s*([^;]+);", block).group(1)
            self.assertIn("vh", value,
                          "%s is not height-responsive: %r" % (prop, value))
        # every clamp() in the sidebar has to be able to shrink
        clamps = re.findall(r"clamp\([^)]*\)", block)
        self.assertTrue(clamps, "sidebar lost its viewport-relative clamps")
        for c in clamps:
            parts = [p.strip() for p in c[len("clamp("):-1].split(",")]
            self.assertEqual(len(parts), 3, "malformed clamp: %s" % c)
            self.assertRegex(parts[1], r"vh$", "clamp midpoint must be vh: %s" % c)
        # the mobile drawer still needs to scroll, so keep the fallback
        self.assertIn("overflow-y: auto", block)

        # the compacting has to reach the whole column, not just the top
        for sel in (".brand", ".nav-label", ".nav a", ".side-cart", ".side-user"):
            s = css.index(sel)
            rule = css[s:css.index("}", s)]
            self.assertIn("vh", rule, "%s does not compact on short screens" % sel)

    def test_sidebar_logo_is_centred_and_marks_the_dashboard_link(self):
        """The Warana Milk wordmark sits in the cream chip at the top of the
        sidebar and has to read as centred, not left-hugging the chip. The
        Dashboard link is the one nav row that carries the logo instead of a
        plain glyph, so both stay locked to the same asset."""
        css = Path(staticfiles_finders.find("bakery/css/style.css")).read_text(
            encoding="utf-8")
        base = get_template("bakery/base.html").template.source

        def rule(sel):
            i = css.index(sel)
            return css[i:css.index("}", i)]

        # the wordmark is 700x350, so the <img> box is wider than the artwork;
        # centring needs both the flex box and object-position
        self.assertIn("justify-content: center", rule(".brand {"))
        self.assertIn("object-position: center", rule(".brand-logo {"))
        self.assertNotIn("left center", rule(".brand-logo {"))

        # exactly one nav row wears the logo, and it is Dashboard
        self.assertEqual(base.count("nav-ico-logo"), 1)
        at = base.index("nav-ico-logo")
        link = base[base.rindex("<a ", 0, at):base.index("</a>", at)]
        self.assertIn("bakery:dashboard", link)
        self.assertIn("bakery/img/logo.png", link)
        self.assertIn("Dashboard", link)
        # the placeholder square glyph is gone from that row
        self.assertNotIn("&#9632;", link)

        # a 2:1 wordmark in a nav row must not stretch the column or the row
        ico = rule(".nav-ico-logo {")
        self.assertIn("flex: none", ico)
        self.assertIn("object-fit: contain", ico)
        self.assertRegex(re.search(r"height:\s*([^;]+);", ico).group(1), r"vh",
                         "the nav logo must compact on short screens")
        self.assertIn("width: auto", ico)

    def test_every_page_shows_the_date_and_time(self):
        """Date and time belong on every screen, not just the dashboard, and
        they must be stamped server-side so the first paint is already right
        before the ticking script takes over."""
        from django.utils import timezone as dj_tz

        self.client.force_login(self.user)
        pages = ["/", reverse("bakery:products"), reverse("bakery:inventory"),
                 reverse("bakery:browse"), reverse("bakery:cart"),
                 reverse("bakery:bills"), reverse("bakery:reports")]
        for url in pages:
            before = dj_tz.localtime()
            html = self.client.get(url).content.decode()
            after = dj_tz.localtime()

            self.assertIn("data-clock", html, "%s has no clock" % url)
            self.assertIn("data-clock-date", html, url)
            self.assertIn("data-clock-time", html, url)
            # a rendered time of its own, and it has to be the real one
            self.assertRegex(html, r'data-clock-time>\d{2}:\d{2}:\d{2}<')
            minutes = {before.strftime("%H:%M"), after.strftime("%H:%M")}
            self.assertTrue(any(m in html for m in minutes),
                            "%s shows a stale clock" % url)
            self.assertIn(before.strftime("%d %b %Y"), html, url)

    def test_the_login_page_also_carries_the_clock(self):
        """login.html does not extend base.html, so the stamp has to be added
        there too -- and app.js has to be loaded for it to tick."""
        html = self.client.get(reverse("login")).content.decode()
        self.assertIn("data-clock", html)
        self.assertIn("data-clock-date", html)
        self.assertIn("data-clock-time", html)
        self.assertIn("bakery/js/app.js", html)

    def test_the_clock_ticks_and_hides_itself_when_printing(self):
        js = Path(staticfiles_finders.find("bakery/js/app.js")).read_text(
            encoding="utf-8")
        self.assertIn("function initClock()", js)
        self.assertIn("setInterval(tick, 1000)", js)
        self.assertIn("initClock();", js)  # actually wired into the boot block

        css = Path(staticfiles_finders.find("bakery/css/style.css")).read_text(
            encoding="utf-8")
        print_css = css[css.index("@media print"):]
        self.assertIn(".page-clock", print_css)
        self.assertIn("display: none", print_css.split(".page-clock")[1][:60])
        # a printed ledger must not stay trapped in the on-screen scroll box
        self.assertIn(".table-scroll", print_css)
        self.assertIn("thead { display: table-header-group", print_css)

    def test_wheat_sprig_decorates_the_login_corner_safely(self):
        """The wheat sprig is decoration only: it must be a real asset, sit
        behind the card rather than over the form, ignore clicks, and never
        be able to widen the page on a phone."""
        css = Path(staticfiles_finders.find("bakery/css/login.css")).read_text(
            encoding="utf-8")
        self.assertIn("../img/wheat.webp", css)
        self.assertIsNotNone(staticfiles_finders.find("bakery/img/wheat.webp"))

        # the sprig hangs off the right of the login panel
        start = css.index(".login-section::after {")
        block = css[start:css.index("}", start)]
        self.assertIn('content: ""', block)
        # it must never intercept clicks over the form
        self.assertIn("pointer-events: none", block)
        # and it is decoration, so it stays subtle rather than shouting
        op = re.search(r"opacity:\s*\.?([\d.]+)", block)
        self.assertIsNotNone(op, "sprig opacity not found")
        self.assertLessEqual(float(op.group(1)), 1.0)

        # the card is lifted above the sprig, so no field can be covered
        card = css[css.index(".login-card {"):css.index("}", css.index(".login-card {"))]
        self.assertRegex(card, r"position:\s*relative")
        self.assertRegex(card, r"z-index:\s*3")
        self.assertNotRegex(block, r"z-index:\s*[4-9]")

        # the sprig deliberately overhangs the right edge; the page has to
        # clip it, or phones grow a horizontal scrollbar
        self.assertRegex(css, r"\.page\s*\{[^}]*overflow-x:\s*hidden")
        self.assertRegex(css, r"\.login-section\s*\{[^}]*position:\s*relative")

        # it is a transparent cut-out, not a rectangular plate
        with Image.open(staticfiles_finders.find("bakery/img/wheat.webp")) as im:
            self.assertEqual(im.format, "WEBP")
            self.assertIn("A", im.mode, "wheat sprig needs an alpha channel")
            alpha = im.convert("RGBA").getchannel("A")
            self.assertEqual(alpha.getextrema()[0], 0,
                             "sprig must have fully transparent pixels")
            self.assertLess(im.width / im.height, 1.2,
                            "sprig should be wider than it is tall")

    def test_login_fits_on_a_laptop_screen_without_scrolling(self):
        """The design is proportioned for a ~1000px tall window. A 1366x768 or
        1280x720 laptop must still see the whole login without scrolling, so
        the three page bands are viewport-aware and a short-screen block
        compacts the card."""
        css = Path(staticfiles_finders.find("bakery/css/login.css")).read_text(
            encoding="utf-8")

        def rule(sel):
            i = css.index(sel)
            return css[i:css.index("}", i)]

        for sel in (".header {", ".main {", ".hero {", ".features {"):
            value = re.search(r"(?:height|min-height):\s*([^;]+);", rule(sel))
            self.assertIsNotNone(value, "%s has no height" % sel)
            self.assertIn("vh", value.group(1),
                          "%s is not height-responsive: %r" % (sel, value.group(1)))

        # a plain fixed 155px/720px/125px stack is 1000px and would scroll
        for sel, fixed in ((".header {", "155px"), (".main {", "720px"),
                           (".features {", "125px")):
            self.assertNotRegex(rule(sel), r"(?:min-)?height:\s*%s\s*;" % fixed,
                                "%s went back to a fixed %s" % (sel, fixed))

        short = css[css.index("@media (max-height: 1010px)"):]
        for sel in (".login-card {", ".input-box {", ".login-btn {",
                    ".features {"):
            self.assertIn(sel, short,
                          "short screens never compact %s" % sel)
        # the breakpoint has to sit above the full-size design's own height,
        # or a ~900px window renders the tall layout and scrolls
        self.assertIn("max-height: 1010px", css)
        self.assertIn("flex: 1 0 auto", rule(".main {"))
        self.assertIn("flex-direction: column", rule(".page {"))

    def test_login_next_refuses_to_redirect_off_site(self):
        """"?next=" must never bounce a freshly logged-in user to another
        host - that is an open redirect."""
        for hostile in ("https://evil.example/steal", "//evil.example/steal",
                        "http://127.0.0.1.evil.example/"):
            with self.subTest(next=hostile):
                self.client.logout()
                response = self.client.post(reverse("login"), {
                    "username": self.user.username, "password": "pw",
                    "next": hostile,
                })
                self.assertEqual(response.status_code, 302,
                                 "login itself must still work")
                self.assertEqual(response["Location"], "/",
                                 "hostile next must fall back to the dashboard")

        response = self.client.post(reverse("login"), {
            "username": self.user.username, "password": "pw",
            "next": reverse("bakery:dashboard"),
        })
        self.assertEqual(response["Location"], reverse("bakery:dashboard"))

    def test_login_page_shows_errors_and_keeps_the_username(self):
        response = self.client.post(reverse("login"), {
            "username": "admin", "password": "wrong",
        })
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("form-error", html)
        self.assertIn("admin", html)  # username is preserved, not cleared

    def test_remember_me_controls_whether_the_cookie_persists(self):
        """Asserted on the real cookie, not get_expiry_age(): Django's
        get_expiry_age() returns the default age for a 0 (browser-close)
        expiry, so it cannot tell the two cases apart."""
        creds = {"username": self.user.username, "password": "pw"}

        self.client.post(reverse("login"), {**creds, "remember": "1"})
        # truthiness, not [...]: a browser-close cookie has no usable max-age
        self.assertTrue(
            self.client.cookies["sessionid"].get("max-age"),
            "remember me should set a persistent cookie")

        self.client.post(reverse("logout"))
        self.client.post(reverse("login"), creds)
        self.assertFalse(
            self.client.cookies["sessionid"].get("max-age"),
            "without remember me the cookie should expire on browser close")

    def test_eye_toggle_and_static_assets_exist(self):
        html = self.client.get("/login/").content.decode()
        self.assertIn('id="eyeIcon"', html)
        self.assertIn("login.js", html)
        for asset in ("bakery/css/login.css", "bakery/js/login.js"):
            with self.subTest(asset=asset):
                self.assertIsNotNone(staticfiles_finders.find(asset))

    def test_logo_file_is_a_valid_wide_png(self):
        from django.contrib.staticfiles import finders
        path = finders.find("bakery/img/logo.png")
        self.assertIsNotNone(path, "logo.png is missing from static files")
        with Image.open(path) as im:
            self.assertEqual(im.format, "PNG")
            self.assertGreater(im.width / im.height, 1.8, "expected a wide lockup")
            self.assertIn("A", im.mode, "logo should have an alpha channel")
            # A transparent cut-out is what lets the same file sit on the
            # cream login header and the dark sidebar. A fully opaque canvas
            # would show as a cream or black rectangle instead.
            alpha = im.convert("RGBA").getchannel("A")
            lo, _ = alpha.getextrema()
            self.assertEqual(lo, 0, "logo must have fully transparent pixels")
            self.assertLess(
                sum(1 for v in alpha.getdata() if v > 8) / (im.width * im.height),
                0.75, "logo looks like an opaque plate rather than a cut-out")

    def test_every_page_title_carries_the_brand(self):
        self.client.force_login(self.user)
        urls = [reverse("bakery:dashboard"), reverse("bakery:browse"),
                reverse("bakery:products"), reverse("bakery:product_create"),
                reverse("bakery:inventory"), reverse("bakery:cart"),
                reverse("bakery:checkout"), reverse("bakery:bills"),
                reverse("bakery:reports")]
        for url in urls:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
                self.assertIn("Warana Milk", title)
                self.assertNotIn("Warana Milk &middot; Warana Milk", title)
                self.assertIsNone(re.search(r"(?i)gokul", html))

    def test_list_pages_do_not_scale_queries_with_row_count(self):
        """A real N+1 shows up as more queries once more rows exist."""
        cat = Category.objects.create(name="PerfCat")

        def build(start, stop):
            for i in range(start, stop):
                p = Product.objects.create(
                    name=f"Perf {i}", category=cat, price=Decimal("10.00"), unit="pcs"
                )
                Inventory.objects.create(product=p, quantity=5, reorder_level=2)
                cart = Cart.objects.create(session_key=f"perf-{i}")
                CartItem.objects.create(cart=cart, product=p, quantity=1)
                billing.checkout(cart, cashier=self.user)

        self.client.force_login(self.user)
        urls = [reverse("bakery:browse"), reverse("bakery:products"),
                reverse("bakery:inventory"), reverse("bakery:bills"),
                reverse("bakery:dashboard"), reverse("bakery:reports")]

        build(0, 3)
        self._measure(urls)  # warm up: creates the session + cart, which is
                             # a one-off INSERT, not a per-row query

        small = self._measure(urls)
        build(3, 9)  # double the rows
        large = self._measure(urls)

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(
                    small[url], large[url],
                    f"{url} issued more queries when rows were added (N+1).",
                )

    def _measure(self, urls):
        counts = {}
        for url in urls:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(url)
            counts[url] = len(ctx.captured_queries)
        return counts


class PhoneValidationTests(TestCase):
    """Requirement 4: a phone number is exactly 10 digits, and a wrong length
    gets a message that says so rather than a silent save."""

    def test_customer_form_accepts_exactly_10_digits(self):
        form = CustomerForm(data={"name": "Ravi", "phone": "9876543210",
                                  "email": "", "address": ""})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["phone"], "9876543210")

    def test_customer_form_rejects_short_and_long_numbers(self):
        for bad in ("123456789", "12345678901"):
            form = CustomerForm(data={"name": "Ravi", "phone": bad,
                                     "email": "", "address": ""})
            self.assertFalse(form.is_valid())
            error = "".join(str(e) for e in form.errors["phone"])
            self.assertIn("must be exactly 10 digits", error, error)
            self.assertIn(bad, error, "length message should quote the input")

    def test_customer_form_rejects_letters(self):
        form = CustomerForm(data={"name": "Ravi", "phone": "abcd98765"})
        self.assertFalse(form.is_valid())
        self.assertIn("digits only", "".join(str(e) for e in form.errors["phone"]))

    def test_customer_form_tolerates_spacing_and_country_code(self):
        form = CustomerForm(data={"name": "Ravi", "phone": "+91 98765 43210"})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["phone"], "9876543210")

    def test_supplier_form_enforces_10_digits(self):
        form = SupplierForm(data={"name": "ACM E", "contact_person": "",
                                  "phone": "9876", "email": "",
                                  "gst_number": "", "address": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("must be exactly 10 digits",
                      "".join(str(e) for e in form.errors["phone"]))

    def test_checkout_rejects_a_wrong_length_phone(self):
        self.client.force_login(User.objects.create_user("ph", password="x"))
        self.client.post(reverse("bakery:cart_add", args=[make_product("Bread", "10", 5).pk]),
                         {"quantity": 1})
        response = self.client.post(reverse("bakery:checkout"), {
            "customer_name": "Ravi", "customer_phone": "12345",
            "discount": "0", "tax_rate": "5", "method": "CASH", "reference": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be exactly 10 digits")
        self.assertEqual(Bill.objects.count(), 0)  # nothing was billed


class ProductRuleTests(TestCase):
    """Requirements 1-3, 5-7: unit is a fixed choice, tax follows the
    category, and a product can carry an expiry date and description."""

    def setUp(self):
        self.user = User.objects.create_user("prod", password="x")
        self.client.force_login(self.user)

    def test_unit_is_a_fixed_dropdown(self):
        cat = Category.objects.create(name="Fish")
        html = self.client.get(reverse("bakery:product_create")).content.decode()
        for label in ("kg", "litre", "pcs"):
            self.assertIn(label, html)
        r = self.client.post(reverse("bakery:product_create"), {
            "name": "Sardine", "category": cat.pk, "price": "50",
            "unit": "fortnight"  # not in UNIT_CHOICES
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not one of the available choices")
        self.assertFalse(Product.objects.filter(name="Sardine").exists())

    def test_tax_follows_the_category_when_left_blank(self):
        cat = Category.objects.create(name="GST12", tax_rate=Decimal("12.00"))
        form = ProductForm(data={
            "name": "Soda", "category": cat.pk, "price": "30",
            "unit": "bottle", "description": "Fizzy",
        })
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()
        self.assertEqual(product.tax_rate, Decimal("12.00"))
        self.assertEqual(product.category.tax_rate, Decimal("12.00"))

    def test_product_form_and_list_accept_an_expiry_date(self):
        from datetime import date
        cat = Category.objects.create(name="Dairy")
        p = Product.objects.create(name="Milk", category=cat, price=Decimal("28"),
                                   unit="pcs", expiry_date=date(2027, 1, 1))
        self.assertEqual(p.expiry_state, "ok")

        r = self.client.post(reverse("bakery:product_create"), {
            "name": "Curd", "category": cat.pk, "price": "38", "unit": "pcs",
            "description": "Thick and fresh", "expiry_date": "2027-01-15",
        })
        self.assertEqual(r.status_code, 302)
        saved = Product.objects.get(name="Curd")
        self.assertEqual(saved.expiry_date.isoformat(), "2027-01-15")
        self.assertEqual(saved.description, "Thick and fresh")

        html = self.client.get(reverse("bakery:products")).content.decode()
        self.assertIn("15 Jan 2027", html)
        self.assertIn(saved.get_unit_display(), html)

    def test_product_tax_rate_shows_on_the_list(self):
        cat = Category.objects.create(name="GST18", tax_rate=Decimal("18.00"))
        Product.objects.create(name="Cake", category=cat, price=Decimal("100"),
                               unit="pcs", tax_rate=Decimal("18.00"))
        html = self.client.get(reverse("bakery:products")).content.decode()
        self.assertIn("18.00%", html)

    def test_low_and_out_of_stock_statuses_are_visible_on_product_and_inventory_pages(self):
        low = make_product("Low Stock Milk", "30.00", 6)
        out = make_product("Out of Stock Milk", "30.00", 0)
        stocked = make_product("Stocked Milk", "30.00", 11)

        products = self.client.get(reverse("bakery:products"))
        self.assertContains(products, "Low Stock! Only 6 units remaining.")
        self.assertContains(products, "OUT OF STOCK")
        self.assertContains(products, "IN STOCK")
        self.assertNotContains(products, "Low Stock! Only 11 units remaining.")

        inventory = self.client.get(reverse("bakery:inventory"))
        self.assertContains(inventory, "Low Stock! Only 6 units remaining.")
        self.assertContains(inventory, "OUT OF STOCK")
        self.assertEqual(low.stock_status_label, "Low Stock")
        self.assertEqual(out.stock_status_label, "Out of Stock")
        self.assertEqual(stocked.stock_status_label, "In Stock")


def purchase_post(reference, supplier, product, qty="10", cost="40",
                  tax="5", expiry="", purchase_date="2026-01-10", notes=""):
    """A complete, valid purchase POST for the create view, plus blank extras."""
    return {
        "supplier": supplier.pk,
        "reference": reference,
        "purchase_date": purchase_date,
        "notes": notes,
        "items-TOTAL_FORMS": "2",
        "items-INITIAL_FORMS": "0",
        "items-MIN_NUM_FORMS": "0",
        "items-MAX_NUM_FORMS": "1000",
        "items-0-product": product.pk,
        "items-0-quantity": qty,
        "items-0-unit_cost": cost,
        "items-0-tax_rate": tax,
        "items-0-expiry_date": expiry,
        "items-1-product": "",
        "items-1-quantity": "",
        "items-1-unit_cost": "",
        "items-1-tax_rate": "",
        "items-1-expiry_date": "",
    }


class PurchaseTests(TestCase):
    """Requirement 8 (purchase validation) plus the stock / expiry side effects."""

    def setUp(self):
        self.user = User.objects.create_user("buyer", password="x")
        self.client.force_login(self.user)
        self.supplier = Supplier.objects.create(name="Dairy Ltd", phone="9822000000")
        self.cat = Category.objects.create(name="Dairy", tax_rate=Decimal("5.00"))
        self.product = Product.objects.create(
            name="Milk", category=self.cat, price=Decimal("28"), unit="pcs",
            tax_rate=Decimal("5.00"),
        )
        Inventory.objects.create(product=self.product, quantity=10)

    def _post(self, **overrides):
        data = purchase_post("INV-001", self.supplier, self.product)
        for src, dst in {
            "expiry": "items-0-expiry_date",
            "qty": "items-0-quantity",
            "cost": "items-0-unit_cost",
        }.items():
            if src in overrides:
                overrides[dst] = overrides.pop(src)
        data.update(overrides)
        return self.client.post(reverse("bakery:purchase_create"), data)

    def test_record_purchase_adds_stock_and_records_expiry(self):
        from datetime import date
        response = self._post(expiry="2026-02-01")
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])

        purchase = Purchase.objects.get(reference="INV-001")
        self.assertEqual(purchase.supplier, self.supplier)
        self.assertEqual(purchase.total, Decimal("420.00"))  # 10 x 40 + 5% tax
        self.assertEqual(purchase.tax_amount, Decimal("20.00"))
        self.assertEqual(purchase.unit_count, 10)
        self.assertEqual(PurchaseItem.objects.get().expiry_date.isoformat(), "2026-02-01")

        self.product.refresh_from_db()
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 20)
        self.assertEqual(self.product.expiry_date, date(2026, 2, 1))

    def test_purchase_form_defaults_to_warana_milk_and_shows_total_preview(self):
        default = Supplier.default_supplier()
        self.assertEqual(Supplier.default_supplier().pk, default.pk)
        response = self.client.get(reverse("bakery:purchase_create"))
        self.assertContains(response, "Purchase From (Supplier)")
        self.assertContains(response, 'id="purchase-subtotal"')
        self.assertContains(response, 'id="purchase-tax"')
        self.assertContains(response, 'id="purchase-total"')
        self.assertContains(response, f'<option value="{default.pk}" selected>')
        self.assertEqual(
            Supplier.objects.filter(name__iexact=Supplier.DEFAULT_NAME).count(), 1
        )

    def test_purchase_validation_messages(self):
        # empty lines
        response = self.client.post(reverse("bakery:purchase_create"), {
            "supplier": self.supplier.pk, "reference": "INV-2",
            "purchase_date": "2026-01-10", "notes": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-product": "", "items-0-quantity": "",
            "items-0-unit_cost": "", "items-0-tax_rate": "",
            "items-0-expiry_date": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least one product line")
        self.assertEqual(Purchase.objects.count(), 0)

        response = self._post(reference="INV-3", expiry="2025-12-01")
        self.assertEqual(response.status_code, 200)  # expiry before purchase date
        self.assertContains(response, "is before the purchase date")

        response = self._post(reference="", expiry="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invoice number")
        self.assertEqual(Purchase.objects.count(), 0)

        response = self._post(reference="INV-4", qty="0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Purchase.objects.count(), 0)

    def test_duplicate_invoice_number_is_rejected(self):
        from django.test import override_settings
        self._post(reference="INV-DUP")
        self.assertEqual(Purchase.objects.filter(reference="INV-DUP").count(), 1)

        form = PurchaseForm(data={
            "supplier": self.supplier.pk, "reference": "INV-DUP",
            "purchase_date": "2026-01-10", "notes": "",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("already been recorded", "".join(str(e) for e in form.errors["reference"]))
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 20)

    def test_future_purchase_date_is_rejected(self):
        from datetime import timedelta
        from django.utils import timezone
        future = (timezone.localdate() + timedelta(days=5)).isoformat()
        response = self._post(reference="INV-FUT", purchase_date=future)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be in the future")
        self.assertEqual(Purchase.objects.count(), 0)

    def test_duplicate_product_lines_are_rejected(self):
        data = purchase_post("INV-5", self.supplier, self.product)
        data["items-TOTAL_FORMS"] = "2"
        data["items-1-product"] = self.product.pk
        data["items-1-quantity"] = "5"
        data["items-1-unit_cost"] = "40"
        data["items-1-tax_rate"] = "5"
        response = self.client.post(reverse("bakery:purchase_create"), data)
        self.assertContains(response, "appears on more than one line")
        self.assertEqual(Purchase.objects.filter(reference="INV-5").count(), 0)

    def test_deleting_a_purchase_gives_the_stock_back(self):
        self._post(reference="INV-6", expiry="2026-03-01")
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 20)
        purchase = Purchase.objects.get(reference="INV-6")

        self.client.post(reverse("bakery:purchase_delete", args=[purchase.pk]))
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 10)
        self.assertFalse(Purchase.objects.filter(reference="INV-6").exists())

    def test_editing_a_purchase_applies_only_the_corrected_stock_quantity(self):
        self._post(reference="INV-EDIT")
        purchase = Purchase.objects.get(reference="INV-EDIT")
        line = purchase.items.get()
        data = purchase_post(
            "INV-EDIT", self.supplier, self.product, qty="15", cost="40",
            purchase_date="2026-01-10",
        )
        data.update({
            "items-TOTAL_FORMS": "4",
            "items-INITIAL_FORMS": "1",
            "items-0-id": str(line.pk),
        })
        for index in (2, 3):
            for field in ("product", "quantity", "unit_cost", "tax_rate", "expiry_date"):
                data[f"items-{index}-{field}"] = ""

        response = self.client.post(
            reverse("bakery:purchase_edit", args=[purchase.pk]), data
        )
        self.assertEqual(response.status_code, 302, response.content.decode()[:500])
        purchase.refresh_from_db()
        self.assertEqual(Purchase.objects.get(reference="INV-EDIT").pk, purchase.pk)
        updated_line = purchase.items.get()
        self.assertEqual(updated_line.pk, line.pk)
        self.assertEqual(updated_line.quantity, 15)
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 25)


class SupplierViewTests(TestCase):
    """Requirements: add and manage suppliers, with 10-digit phone checks."""

    def setUp(self):
        self.user = User.objects.create_user("su", password="x")
        self.client.force_login(self.user)

    def test_supplier_list_and_quick_add(self):
        response = self.client.get(reverse("bakery:suppliers"))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(reverse("bakery:suppliers"), {
            "name": "Warana Mills", "contact_person": "Anil",
            "phone": "9822001111", "gst_number": "27AAACV1234A1Z5",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Supplier.objects.filter(name="Warana Mills").exists())

    def test_quick_add_rejects_a_bad_phone(self):
        response = self.client.post(reverse("bakery:suppliers"), {
            "name": "Bad Phone", "contact_person": "", "phone": "123",
            "gst_number": "", "email": "", "address": "",
        }, follow=True)
        self.assertContains(response, "must be exactly 10 digits")
        self.assertFalse(Supplier.objects.filter(name="Bad Phone").exists())

    def test_warana_milk_default_cannot_be_duplicated(self):
        default = Supplier.default_supplier()
        response = self.client.post(reverse("bakery:suppliers"), {
            "name": "warana milk", "contact_person": "Warana Milk",
            "phone": "9876543210", "email": "", "gst_number": "",
            "address": "",
        }, follow=True)
        self.assertContains(response, "already exists")
        self.assertTrue(Supplier.objects.filter(pk=default.pk).exists())
        self.assertEqual(
            Supplier.objects.filter(name__iexact=Supplier.DEFAULT_NAME).count(), 1
        )

    def test_supplier_with_purchases_is_deactivated_not_deleted(self):
        supplier = Supplier.objects.create(name="Ten G", phone="9822002222")
        cat = Category.objects.create(name="X", tax_rate=Decimal("5"))
        product = Product.objects.create(name="P", category=cat, price=Decimal("10"))
        Inventory.objects.create(product=product, quantity=5)
        purchasing.record_purchase(
            reference="INV-S1", supplier=supplier, purchase_date="2026-01-01",
            lines=[{"product": product, "quantity": 5, "unit_cost": Decimal("8"),
                    "tax_rate": Decimal("5")}],
        )

        response = self.client.post(reverse("bakery:supplier_delete", args=[supplier.pk]),
                                    follow=True)
        supplier.refresh_from_db()
        self.assertFalse(supplier.is_active)
        self.assertContains(response, "deactivated")
        self.assertTrue(Supplier.objects.filter(pk=supplier.pk).exists())


class FormValidationTests(TestCase):
    """Every renderable form shows its messages: field errors AND non-field
    errors are both on the page, and the remaining free-text inputs are
    shape-checked."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("fv", password="x")
        cls.cat = Category.objects.create(name="Bakery", tax_rate=Decimal("18.00"))

    def _login_with_cart(self):
        self.client.force_login(self.user)
        self.client.post(reverse("bakery:cart_add", args=[self.cart_product().pk]),
                         {"quantity": 1})

    def cart_product(self):
        product, _ = Product.objects.get_or_create(
            name="Cupcake", defaults={"category": self.cat, "price": Decimal("40"),
                                      "unit": "pcs"},
        )
        Inventory.objects.get_or_create(product=product, defaults={"quantity": 10})
        return product

    def test_checkout_shows_both_or_neither_message(self):
        self._login_with_cart()
        response = self.client.post(reverse("bakery:checkout"), {
            "customer_name": "Ravi", "customer_phone": "",
            "discount": "0", "tax_rate": "5", "method": "CASH", "reference": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Enter both customer name and phone, or leave both blank."
        )
        self.assertEqual(Bill.objects.count(), 0)

    def test_checkout_phone_error_sits_on_the_field(self):
        self._login_with_cart()
        response = self.client.post(reverse("bakery:checkout"), {
            "customer_name": "Ravi", "customer_phone": "12345",
            "discount": "0", "tax_rate": "5", "method": "CASH", "reference": "",
        })
        self.assertContains(response, "must be exactly 10 digits")
        self.assertContains(response, 'name="customer_phone"')

    def test_gst_number_shape_is_checked(self):
        form = SupplierForm(data={
            "name": "GST Co", "contact_person": "", "phone": "9822004444",
            "gst_number": "27AAACV1234A1",  # too short
            "email": "", "address": "",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("15 characters",
                      "".join(str(e) for e in form.errors["gst_number"]))

        form = SupplierForm(data={
            "name": "GST Co", "contact_person": "", "phone": "9822004444",
            "gst_number": "27aaacv1234a1z5",  # valid, needs capitalising
            "email": "", "address": "",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["gst_number"], "27AAACV1234A1Z5")

    def test_product_image_url_must_be_http(self):
        form = ProductForm(data={
            "name": "Imaged", "category": self.cat.pk, "price": "12",
            "unit": "pcs", "image_url": "javascript:alert(1)",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("http://", "".join(str(e) for e in form.errors["image_url"]))

        form = ProductForm(data={
            "name": "Imaged", "category": self.cat.pk, "price": "12",
            "unit": "pcs", "image_url": "https://example.com/pic.jpg",
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_purchase_item_date_survives_a_string_purchase_date(self):
        product = self.cart_product()
        formset = PurchaseItemFormSet(
            {"items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0",
             "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
             "items-0-product": product.pk, "items-0-quantity": "2",
             "items-0-unit_cost": "30", "items-0-tax_rate": "5",
             "items-0-expiry_date": "2025-01-01"},
            form_kwargs={"purchase_date": "2026-01-10"},
        )
        # a string date must never crash the comparative check
        self.assertFalse(formset.is_valid())


class ReportTests(TestCase):
    """The report library: customer, product, stock and bill reports with CSV."""

    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone
        from .models import BillItem, Customer

        cls.user = User.objects.create_user("reporter", password="x")
        cls.cake = Category.objects.create(
            name="Cake", description="Freshly baked cakes for every celebration.",
            tax_rate=Decimal("18.00"),
        )
        cls.dairy = Category.objects.create(
            name="Dairy", description="Milk, curd, paneer and cold items.",
            tax_rate=Decimal("5.00"),
        )
        cls.cake_product = Product.objects.create(
            name="Chocolate Cake", category=cls.cake, price=Decimal("500.00"),
            unit="pcs", tax_rate=Decimal("18.00"),
        )
        cls.milk_product = Product.objects.create(
            name="Toned Milk", category=cls.dairy, price=Decimal("28.00"),
            unit="ml", tax_rate=Decimal("5.00"),
        )
        Inventory.objects.create(product=cls.cake_product, quantity=8)
        Inventory.objects.create(product=cls.milk_product, quantity=0)

        cls.customer = Customer.objects.create(
            name="Ravi Kumar", phone="9800000001",
            email="ravi@example.com", address="12 Dairy Road",
        )
        cls.bill = Bill.objects.create(
            bill_number="WRN-20260101-0001", customer=cls.customer,
            cashier=cls.user, subtotal=Decimal("1000.00"),
            tax_amount=Decimal("180.00"),             total=Decimal("1180.00"),
            status="PAID", created_at=timezone.now(),
        )
        BillItem.objects.create(
            bill=cls.bill, product=cls.cake_product, product_name="Chocolate Cake",
            quantity=2, unit_price=Decimal("500.00"), line_total=Decimal("1000.00"),
            tax_rate=Decimal("18.00"), tax_amount=Decimal("180.00"),
        )
        Payment.objects.create(
            bill=cls.bill, method="CASH", amount=Decimal("1180.00"),
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_report_pages_and_csv_exports(self):
        pages = [
            reverse("bakery:report_customers"),
            reverse("bakery:report_products"),
            reverse("bakery:report_stock"),
            reverse("bakery:report_bills"),
        ]
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Warana Milk")
                self.assertContains(response, "Download PDF")

        customer_html = self.client.get(reverse("bakery:report_customers")).content.decode()
        self.assertIn("Ravi Kumar", customer_html)
        self.assertIn("9800000001", customer_html)
        self.assertIn("#%s" % self.customer.pk, customer_html)

        product_html = self.client.get(reverse("bakery:report_products")).content.decode()
        for text in ("Chocolate Cake", "Cake", "500.00", "pcs", "Rate / Unit"):
            self.assertIn(text, product_html)

        stock_html = self.client.get(reverse("bakery:report_stock")).content.decode()
        for text in ("Toned Milk", "Stock Value", "OUT OF STOCK"):
            self.assertIn(text, stock_html)

        bill_html = self.client.get(reverse("bakery:report_bills")).content.decode()
        for text in ("WRN-20260101-0001", "Ravi Kumar", "1180.00", "Cash"):
            self.assertIn(text, bill_html)
        self.assertIn(reverse("bakery:receipt", args=[self.bill.pk]), bill_html)
        self.assertIn(reverse("bakery:receipt_pdf", args=[self.bill.pk]), bill_html)

    def test_filtered_report_pdfs_download_with_letterhead(self):
        cases = [
            ("report_customers_pdf", {"q": "Ravi"}, "Ravi Kumar"),
            ("report_products_pdf", {"category": self.cake.pk}, "Chocolate Cake"),
            ("report_stock_pdf", {"status": "out"}, "Toned Milk"),
            ("report_bills_pdf", {
                "from": self.bill.created_at.strftime("%Y-%m-%d"),
                "to": self.bill.created_at.strftime("%Y-%m-%d"),
                "status": "PAID", "q": self.bill.bill_number,
            }, self.bill.bill_number),
        ]
        for name, filters, expected in cases:
            with self.subTest(pdf=name):
                response = self.client.get(
                    reverse("bakery:%s" % name), filters
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertIn("attachment", response["Content-Disposition"])
                self.assertTrue(response.content.startswith(b"%PDF-"))
                self.assertIn(b"/Subtype /Image", response.content)
                text = pdf_text(response.content)
                self.assertIn("WARANA MILK", text)
                self.assertIn(expected, text)

    def test_bill_report_receipt_has_letterhead_customer_details_and_print_actions(self):
        receipt_html = self.client.get(
            reverse("bakery:receipt", args=[self.bill.pk])
        ).content.decode()
        for text in (
            "Warana Milk", "TAX INVOICE", "Main Road, Warana, Maharashtra",
            "98765 43210", "Ravi Kumar", "9800000001", "ravi@example.com",
            "12 Dairy Road", "Chocolate Cake", "Quantity", "Rate", "GST / Tax",
            "Grand Total", "Payment Status", "Authorised Signatory",
            "Print Bill", "Download PDF",
        ):
            self.assertIn(text, receipt_html)

        pdf = self.client.get(reverse("bakery:receipt_pdf", args=[self.bill.pk]))
        self.assertEqual(pdf.status_code, 200)
        text = pdf_text(pdf.content)
        for value in ("WARANA MILK", "TAX INVOICE", "Ravi Kumar", "9800000001",
                      "ravi@example.com", "12 Dairy Road", "Chocolate Cake",
                      "Quantity", "Rate", "Grand Total", "Payment Status",
                      "Authorised Signatory"):
            self.assertIn(value, text)

        cases = [
            ("report_customers_csv", "Customer ID", "Ravi Kumar"),
            ("report_products_csv", "Product ID", "Chocolate Cake"),
            ("report_stock_csv", "Stock Value", "Toned Milk"),
            ("report_bills_csv", "Bill No", "WRN-20260101-0001"),
        ]
        for name, header, value in cases:
            with self.subTest(csv=name):
                response = self.client.get(reverse("bakery:%s" % name))
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/csv", response["Content-Type"])
                text = response.content.decode("utf-8-sig")
                self.assertIn(header, text)
                self.assertIn(value, text)

    def test_reports_can_be_filtered(self):
        cake_only = self.client.get(
            reverse("bakery:report_products"), {"category": self.cake.pk}
        ).content.decode()
        self.assertIn("Chocolate Cake", cake_only)
        self.assertNotIn("Toned Milk", cake_only)

        out_only = self.client.get(
            reverse("bakery:report_stock"), {"status": "out"}
        ).content.decode()
        self.assertIn("Toned Milk", out_only)
        self.assertNotIn("Chocolate Cake", out_only)

        from datetime import timedelta
        from django.utils import timezone
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        empty = self.client.get(
            reverse("bakery:report_bills"), {"from": tomorrow, "to": tomorrow}
        ).content.decode()
        self.assertIn("No bills found", empty)

    def test_cake_has_a_one_line_description_on_the_product_page(self):
        html = self.client.get(reverse("bakery:products")).content.decode()
        self.assertIn("Category Overview", html)
        self.assertIn("Freshly baked cakes for every celebration.", html)

    def test_the_brand_is_spelled_warana_everywhere(self):
        self.client.logout()
        login_html = self.client.get(reverse("login")).content.decode()
        self.assertIn("Warana", login_html)
        self.assertNotIn("Express", login_html)
        self.assertNotIn("Varna", login_html)

        self.client.force_login(self.user)
        bill = Bill.objects.create(
            bill_number="WRN-20261116-0001", subtotal=Decimal("50"),
            tax_amount=Decimal("2.50"), total=Decimal("52.50"), status="PAID",
        )
        for url in (reverse("bakery:dashboard"), reverse("bakery:products"),
                    reverse("bakery:reports"), reverse("bakery:report_bills"),
                    reverse("bakery:receipt", args=[bill.pk])):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn("Warana Milk", html)
                self.assertNotIn("Varna", html)
                self.assertNotIn("Express", html)
