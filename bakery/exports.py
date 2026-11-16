"""Billing exports: a printable PDF receipt and a CSV of the bill history.

Both return bytes so the views can hand them to a FileResponse without ever
touching the filesystem.
"""

import csv
import io
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape

from django.contrib.staticfiles import finders
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image as PDFImage, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from .models import Bill

BRAND = colors.HexColor("#A9713C")
GOLD = colors.HexColor("#C99A54")
ESPRESSO = colors.HexColor("#2F2018")
MUTED = colors.HexColor("#7A6A5C")
LINE = colors.HexColor("#E3D9C8")
RUPEE = "\u20b9"

BILLING_FIELDS = [
    "Bill No", "Date", "Customer", "Phone", "Cashier", "Items",
    "Subtotal", "Discount", "Tax", "Total", "Payment", "Status",
]


def _money(value):
    """Rupee amount for text exports (CSV), which are UTF-8 and render fine."""
    return "%s%s" % (RUPEE, Decimal(value or 0).quantize(Decimal("0.01")))


def _pdf_money(value):
    """Amount for the PDF.

    The PDF uses the standard Helvetica base font, whose WinAnsi encoding has
    no rupee sign, so U+20B9 would be emitted as a wrong glyph. "Rs." is what
    every Indian invoice uses anyway and is portable.
    """
    return "Rs. %s" % Decimal(value or 0).quantize(Decimal("0.01"))


def _report_letterhead(width):
    logo_path = finders.find("bakery/img/logo.png")
    if not logo_path:
        raise FileNotFoundError("Required Warana Milk logo is missing from static files.")

    base = getSampleStyleSheet()
    brand = ParagraphStyle(
        "report-letterhead-brand", parent=base["Heading1"], fontSize=15,
        leading=18, textColor=ESPRESSO, spaceAfter=2,
    )
    detail = ParagraphStyle(
        "report-letterhead-detail", parent=base["Normal"], fontSize=8,
        leading=10, textColor=BRAND, spaceAfter=1,
    )
    logo_width = min(36 * mm, width * 0.22)
    logo = PDFImage(logo_path, width=logo_width, height=logo_width / 2)
    company = [
        Paragraph("<b>WARANA MILK</b>", brand),
        Paragraph("Main Road, Warana, Maharashtra", detail),
        Paragraph("Phone: 98765 43210", detail),
    ]
    letterhead = Table([[logo, company]],
                       colWidths=[logo_width + 5 * mm, width - logo_width - 5 * mm])
    letterhead.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FBF7F0")),
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBELOW", (0, 0), (-1, -1), 2, BRAND),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return letterhead


def _tax_label(rate):
    """Match receipt.html, which prints the rate with floatformat:0."""
    return "%s%%" % Decimal(rate or 0).quantize(Decimal("1"),
                                                rounding=ROUND_HALF_UP)


def _local(dt):
    """Bill timestamps are stored in UTC; show them in the shop's timezone."""
    if dt is None:
        return ""
    try:
        from django.utils import timezone as dj_tz
        dt = dj_tz.localtime(dt)
    except (ValueError, TypeError):
        pass
    return dt.strftime("%d-%m-%Y %H:%M")


def receipt_pdf(bill):
    """Render one bill with its Warana Milk letterhead on A4."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=14 * mm,
        title="Receipt %s" % bill.bill_number,
        author="Warana Milk",
    )

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontSize=17,
                        leading=20, textColor=ESPRESSO, spaceAfter=2)
    tag = ParagraphStyle("tag", parent=base["Normal"], fontSize=9,
                         textColor=BRAND, spaceAfter=2)
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8,
                           textColor=MUTED, alignment=1)
    label = ParagraphStyle("label", parent=base["Normal"], fontSize=7.5,
                           leading=10, textColor=MUTED)
    cell = ParagraphStyle("cell", parent=base["Normal"], fontSize=8.5,
                          textColor=ESPRESSO)
    right = ParagraphStyle("right", parent=cell, alignment=TA_RIGHT)
    table_header = ParagraphStyle(
        "table_header", parent=cell, textColor=colors.white,
        fontName="Helvetica-Bold",
    )
    table_header_right = ParagraphStyle(
        "table_header_right", parent=table_header, alignment=TA_RIGHT,
    )

    logo_path = finders.find("bakery/img/logo.png")
    if not logo_path:
        raise FileNotFoundError("Required Warana Milk logo is missing from static files.")
    logo = PDFImage(logo_path, width=43 * mm, height=21.5 * mm)
    company = [
        Paragraph("WARANA MILK", h1),
        Paragraph("Main Road, Warana, Maharashtra", tag),
        Paragraph("Phone: 98765 43210", tag),
        Paragraph("<b>TAX INVOICE</b>", label),
    ]
    letterhead = Table([[logo, company]], colWidths=[50 * mm, 136 * mm])
    letterhead.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FBF7F0")),
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBELOW", (0, 0), (-1, -1), 2, BRAND),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    cashier = ((bill.cashier.get_full_name() or bill.cashier.username)
               if bill.cashier else "-")
    metadata = Table([
        [Paragraph("BILL NUMBER", label), Paragraph("BILL DATE", label),
         Paragraph("CASHIER", label)],
        [Paragraph("<b>%s</b>" % escape(bill.bill_number), cell),
         Paragraph(escape(_local(bill.created_at)), cell),
         Paragraph("<b>%s</b>" % escape(cashier), cell)],
    ], colWidths=[62 * mm, 62 * mm, 62 * mm])
    metadata.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    customer_name = bill.customer.name if bill.customer else "Walk-in Customer"
    customer_details = [
        Paragraph("BILLED TO", label),
        Paragraph("<b>%s</b>" % escape(customer_name), cell),
    ]
    if bill.customer:
        if bill.customer.phone:
            customer_details.append(Paragraph(
                "Phone: %s" % escape(bill.customer.phone), cell
            ))
        if bill.customer.email:
            customer_details.append(Paragraph(
                "Email: %s" % escape(bill.customer.email), cell
            ))
        if bill.customer.address:
            customer_details.append(Paragraph(
                "Address: %s" % escape(bill.customer.address), cell
            ))
    customer_details.append(Paragraph(
        "Payment Status: <b>%s</b>" % escape(bill.get_status_display()), cell
    ))
    customer_box = Table([[customer_details]], colWidths=[186 * mm])
    customer_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FBF7F0")),
        ("BOX", (0, 0), (-1, -1), 0.7, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story = [letterhead, Spacer(1, 8), metadata, Spacer(1, 8), customer_box,
             Spacer(1, 8)]

    rows = [[Paragraph("#", table_header),
             Paragraph("Product Name", table_header),
             Paragraph("Quantity", table_header_right),
             Paragraph("Rate", table_header_right),
             Paragraph("GST / Tax", table_header_right),
             Paragraph("Amount", table_header_right)]]
    for i, item in enumerate(bill.items.select_related("product"), start=1):
        item_rate = getattr(item, "tax_rate", None)
        if item_rate is None:
            item_rate = bill.tax_rate
        item_tax = getattr(item, "tax_amount", None) or Decimal("0")
        rows.append([
            Paragraph(str(i), cell),
            Paragraph(escape(item.product_name), cell),
            Paragraph(str(item.quantity), right),
            Paragraph(_pdf_money(item.unit_price), right),
            Paragraph("%s<br/>%s" % (_tax_label(item_rate), _pdf_money(item_tax)),
                      right),
            Paragraph(_pdf_money(item.line_total), right),
        ])

    lines = [[Paragraph("Subtotal", cell),
              Paragraph(_pdf_money(bill.subtotal), right)],
             [Paragraph("Discount", cell),
              Paragraph("- " + _pdf_money(bill.discount), right)],
             [Paragraph("Tax (%s)" % _tax_label(bill.tax_rate), cell),
              Paragraph(_pdf_money(bill.tax_amount), right)]]
    total = Table([[Paragraph("<b>Grand Total (Total Paid)</b>", cell),
                    Paragraph("<b>%s</b>" % _pdf_money(bill.total), right)]],
                  colWidths=[114 * mm, 72 * mm])
    total.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 1.1, BRAND),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    table = Table(rows, colWidths=[8 * mm, 67 * mm, 22 * mm, 28 * mm,
                                   29 * mm, 32 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ESPRESSO),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#FBF7F0")]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
    ]))
    story += [
        table, Spacer(1, 8),
        Table(lines, colWidths=[114 * mm, 72 * mm], hAlign="RIGHT"), total,
    ]

    payment = bill.payment
    payment_text = "Payment Method: <b>%s</b>" % escape(
        payment.get_method_display() if payment else "-"
    )
    if payment and payment.reference:
        payment_text += " | Reference: %s" % escape(payment.reference)
    if payment and payment.paid_at:
        payment_text += " | Paid at: %s" % escape(_local(payment.paid_at))
    story += [
        Spacer(1, 8), Paragraph(payment_text, cell), Spacer(1, 12),
        Paragraph("Thank you for shopping with Warana Milk!", ParagraphStyle(
            "foot", parent=cell, alignment=1, fontName="Helvetica-Bold",
            textColor=ESPRESSO, spaceAfter=3,
        )),
        Paragraph("We appreciate your business.", small), Spacer(1, 13),
    ]
    signature = Table([
        [Paragraph("For Warana Milk", label)],
        [Paragraph(" ", cell)],
        [Paragraph("Authorised Signatory", label)],
    ], colWidths=[58 * mm], hAlign="RIGHT")
    signature.setStyle(TableStyle([
        ("LINEABOVE", (0, 2), (-1, 2), 0.7, MUTED),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(signature)

    def rule(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1.4)
        canvas.line(12 * mm, 10 * mm, A4[0] - 12 * mm, 10 * mm)
        canvas.restoreState()

    doc.build(story, onFirstPage=rule, onLaterPages=rule)
    return buf.getvalue()


def bills_csv(bills):
    """Bill history as CSV, with a UTF-8 BOM so Excel opens it correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(BILLING_FIELDS)
    for b in bills:
        writer.writerow([
            b.bill_number, _local(b.created_at),
            (b.customer.name if b.customer else "") or "Walk-in",
            (b.customer.phone if b.customer else "") or "",
            (b.cashier.username if b.cashier else ""),
            b.item_count, _money(b.subtotal), _money(b.discount),
            "%s (%s%%)" % (_money(b.tax_amount), b.tax_rate.normalize()),
            _money(b.total),
            (b.payment.get_method_display() if b.payment else "-"),
            b.get_status_display(),
        ])
    return "\ufeff" + buf.getvalue()


CUSTOMER_REPORT_FIELDS = [
    "Customer ID", "Customer", "Phone", "Email", "Bills", "Total Spent",
    "Last Purchase",
]


def customers_csv(customers):
    """Customer report: who buys, how often and how much."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Warana Milk - Customer Report"])
    writer.writerow(CUSTOMER_REPORT_FIELDS)
    for c in customers:
        writer.writerow([
            c.pk, c.name, c.phone, c.email or "", c.bill_count,
            _money(c.spend), _local(c.last_bill),
        ])
    return "\ufeff" + buf.getvalue()


PRODUCT_REPORT_FIELDS = [
    "Product ID", "Product Name", "Category", "Stock", "Rate / Unit", "GST %",
    "Units Sold", "Revenue",
]


def products_csv(products):
    """Product report: master fields plus what each product has sold."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Warana Milk - Product Report"])
    writer.writerow(PRODUCT_REPORT_FIELDS)
    for p in products:
        writer.writerow([
            p.pk, p.name, p.category.name, p.stock,
            "%s / %s" % (_money(p.price), p.get_unit_display()),
            p.tax_rate, p.units_sold, _money(p.revenue),
        ])
    return "\ufeff" + buf.getvalue()


STOCK_REPORT_FIELDS = [
    "Product ID", "Product Name", "Category", "Stock", "Rate / Unit",
    "Stock Value", "Status",
]


def stock_csv(rows):
    """Stock report: the on-hand position for every product."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Warana Milk - Stock Report"])
    writer.writerow(STOCK_REPORT_FIELDS)
    for row in rows:
        product = row.product
        writer.writerow([
            product.pk, product.name, product.category.name, row.quantity,
            "%s / %s" % (_money(product.price), product.get_unit_display()),
            _money(row.stock_value), row.stock_status_label,
        ])
    return "\ufeff" + buf.getvalue()


def bill_report_csv(bills):
    """Bill report for a date range, including the payment mode."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Warana Milk - Bill Report"])
    writer.writerow(BILLING_FIELDS)
    for b in bills:
        writer.writerow([
            b.bill_number, _local(b.created_at),
            (b.customer.name if b.customer else "") or "Walk-in",
            (b.customer.phone if b.customer else "") or "",
            (b.cashier.username if b.cashier else ""),
            b.item_count, _money(b.subtotal), _money(b.discount),
            "%s (%s%%)" % (_money(b.tax_amount), b.tax_rate.normalize()),
            _money(b.total),
            (b.payment.get_method_display() if b.payment else "-"),
            b.get_status_display(),
        ])
    return "\ufeff" + buf.getvalue()


REPORT_ROW_FIELDS = [
    "Period", "Bills", "Sales", "GST", "Revenue", "Purchases",
    "Purchase Total", "Purchase GST",
]


def report_csv(context):
    """Daily / monthly / yearly report as CSV, with the usual Excel BOM."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Warana Milk - %s Report" % context["period"].title()])
    writer.writerow(["Period", context["label"]])
    writer.writerow(["From", context["start"].strftime("%d-%m-%Y")])
    writer.writerow(["To", context["end"].strftime("%d-%m-%Y")])
    writer.writerow([])
    writer.writerow(["Sales", _money(context["sales"]["gross"])])
    writer.writerow(["Discount", _money(context["sales"]["discount"])])
    writer.writerow(["Sales GST", _money(context["sales"]["tax"])])
    writer.writerow(["Revenue", _money(context["sales"]["net"])])
    writer.writerow(["Bills", context["sales"]["bills"]])
    writer.writerow(["Purchases", _money(context["purchases"]["net"])])
    writer.writerow(["Purchase GST", _money(context["purchases"]["tax"])])
    writer.writerow([])
    writer.writerow(REPORT_ROW_FIELDS)
    for row in context["rows"]:
        writer.writerow([
            row["label"], row["bills"], _money(row["sales"]), _money(row["tax"]),
            _money(row["revenue"]), row["purchases"], _money(row["purchase_total"]),
            _money(row["purchase_tax"]),
        ])
    writer.writerow([])
    writer.writerow(["Product", "Units", "Revenue", "GST"])
    for row in context["product_rows"]:
        writer.writerow([
            row["product_name"], row["units"], _money(row["revenue"]),
            _money(row.get("tax", 0)),
        ])
    return "\ufeff" + buf.getvalue()


def report_table_pdf(title, subtitle, headers, rows, summary,
                     column_weights, landscape_page=False):
    """Render a filtered report table with the Warana Milk letterhead."""
    buf = io.BytesIO()
    pagesize = landscape(A4) if landscape_page else A4
    margin = 10 * mm
    width = pagesize[0] - 2 * margin
    doc = SimpleDocTemplate(
        buf, pagesize=pagesize,
        leftMargin=margin, rightMargin=margin,
        topMargin=10 * mm, bottomMargin=14 * mm,
        title="Warana Milk - %s" % title,
        author="Warana Milk",
    )

    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "table-report-title", parent=base["Heading2"], fontSize=12,
        leading=15, textColor=BRAND, spaceBefore=8, spaceAfter=3,
    )
    meta_style = ParagraphStyle(
        "table-report-meta", parent=base["Normal"], fontSize=8,
        textColor=MUTED, spaceAfter=5,
    )
    cell_style = ParagraphStyle(
        "table-report-cell", parent=base["Normal"], fontSize=7,
        leading=8.5, textColor=ESPRESSO,
    )
    header_style = ParagraphStyle(
        "table-report-header", parent=cell_style, fontName="Helvetica-Bold",
        textColor=colors.white,
    )

    table_data = [[Paragraph(escape(str(value)), header_style) for value in headers]]
    for row in rows:
        table_data.append([
            Paragraph(escape(str(value)), cell_style) for value in row
        ])
    if not rows:
        table_data.append([
            Paragraph("No records found.", cell_style)
        ] + [""] * (len(headers) - 1))

    weight_total = sum(column_weights)
    widths = [width * weight / weight_total for weight in column_weights]
    table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#FBF7F0")]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
    ]
    if not rows:
        table_style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(table_style))

    from django.utils import timezone as dj_tz
    generated = dj_tz.localtime().strftime("%d-%m-%Y %H:%M")
    story = [
        _report_letterhead(width),
        Paragraph(escape(title), title_style),
        Paragraph("%s<br/>%s &middot; Generated %s" % (
            escape(subtitle), escape(summary), generated,
        ), meta_style),
        table,
        Spacer(1, 10),
        Paragraph("This is a computer generated report.", meta_style),
    ]

    def rule(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1.4)
        canvas.line(margin, 9 * mm, pagesize[0] - margin, 9 * mm)
        canvas.restoreState()

    doc.build(story, onFirstPage=rule, onLaterPages=rule)
    return buf.getvalue()


def report_pdf(context):
    """Render the selected daily / monthly / yearly report as a PDF."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title="Warana Milk - %s Report" % context["period"].title(),
        author="Warana Milk",
    )

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("report-h1", parent=base["Heading1"], fontSize=17,
                        textColor=ESPRESSO, spaceAfter=0)
    tag = ParagraphStyle("report-tag", parent=base["Normal"], fontSize=9.5,
                         textColor=BRAND, alignment=1, spaceAfter=1)
    small = ParagraphStyle("report-small", parent=base["Normal"], fontSize=8.5,
                           textColor=MUTED, alignment=1, spaceAfter=6)
    cell = ParagraphStyle("report-cell", parent=base["Normal"], fontSize=9,
                          textColor=ESPRESSO)
    right = ParagraphStyle("report-right", parent=cell, alignment=TA_RIGHT)
    h2 = ParagraphStyle("report-h2", parent=base["Normal"], fontSize=9,
                        textColor=MUTED)
    heading = ParagraphStyle("report-heading", parent=base["Heading2"], fontSize=11,
                             textColor=BRAND, spaceBefore=8, spaceAfter=4)

    from django.utils import timezone as dj_tz
    generated = dj_tz.localtime().strftime("%d-%m-%Y %H:%M")

    story = [
        _report_letterhead(A4[0] - 28 * mm),
        Paragraph("%s Report &middot; %s" % (context["period"].title(), context["label"]),
                  heading),
        Paragraph("Generated %s" % generated, small),
    ]

    summary = [
        [Paragraph("Metric", h2), Paragraph("Value", right)],
        [Paragraph("Sales", cell), Paragraph(_pdf_money(context["sales"]["gross"]), right)],
        [Paragraph("Discount", cell), Paragraph(_pdf_money(context["sales"]["discount"]), right)],
        [Paragraph("Sales GST", cell), Paragraph(_pdf_money(context["sales"]["tax"]), right)],
        [Paragraph("Revenue", cell), Paragraph(_pdf_money(context["sales"]["net"]), right)],
        [Paragraph("Bills", cell), Paragraph(str(context["sales"]["bills"]), right)],
        [Paragraph("Purchases", cell), Paragraph(_pdf_money(context["purchases"]["net"]), right)],
        [Paragraph("Purchase GST", cell), Paragraph(_pdf_money(context["purchases"]["tax"]), right)],
    ]
    summary_table = Table(summary, colWidths=[100 * mm, 74 * mm], hAlign="RIGHT")
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#FBF7F0")]),
    ]))
    story += [summary_table, Spacer(1, 8)]

    if context["rows"]:
        row_data = [[Paragraph("Period", h2), Paragraph("Bills", right),
                     Paragraph("Sales", right), Paragraph("GST", right),
                     Paragraph("Revenue", right), Paragraph("Purchases", right)]]
        for row in context["rows"]:
            row_data.append([
                Paragraph(str(row["label"]), cell),
                Paragraph(str(row["bills"]), right),
                Paragraph(_pdf_money(row["sales"]), right),
                Paragraph(_pdf_money(row["tax"]), right),
                Paragraph(_pdf_money(row["revenue"]), right),
                Paragraph(_pdf_money(row["purchase_total"]), right),
            ])
        report_table = Table(
            row_data,
            colWidths=[34 * mm, 18 * mm, 30 * mm, 28 * mm, 32 * mm, 32 * mm],
            repeatRows=1,
        )
        report_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#FBF7F0")]),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ]))
        story += [Paragraph("Period Breakdown", heading), report_table]

    products = list(context["product_rows"][:20])
    if products:
        product_data = [[Paragraph("Product", h2), Paragraph("Units", right),
                         Paragraph("Revenue", right), Paragraph("GST", right)]]
        for row in products:
            product_data.append([
                Paragraph(str(row["product_name"]), cell),
                Paragraph(str(row["units"]), right),
                Paragraph(_pdf_money(row["revenue"]), right),
                Paragraph(_pdf_money(row.get("tax", 0)), right),
            ])
        product_table = Table(
            product_data,
            colWidths=[94 * mm, 22 * mm, 30 * mm, 28 * mm],
            repeatRows=1,
        )
        product_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#FBF7F0")]),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ]))
        story += [Paragraph("Product Sales", heading), product_table]

    story += [Spacer(1, 12), Paragraph(
        "This is a computer generated report.", ParagraphStyle(
            "report-foot", parent=small, alignment=1, spaceAfter=0))]

    def rule(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1.4)
        canvas.line(14 * mm, 10 * mm, A4[0] - 14 * mm, 10 * mm)
        canvas.restoreState()

    doc.build(story, onFirstPage=rule, onLaterPages=rule)
    return buf.getvalue()
