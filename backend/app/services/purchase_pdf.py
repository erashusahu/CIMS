"""Compact printable purchase order, generated from committed order details."""

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

AUBERGINE = colors.HexColor("#34243b")
SILVER = colors.HexColor("#ecf1f2")
MARIGOLD = colors.HexColor("#efb63f")


def render_purchase_order(order, vendor, location, items):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=42,
        leftMargin=42,
        topMargin=40,
        bottomMargin=42,
        title=f"Purchase order {order['po_no']}",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="CimsTitle",
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=23,
            textColor=AUBERGINE,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CimsSmall", fontName="Helvetica", fontSize=9, leading=13, textColor=AUBERGINE
        )
    )
    styles.add(ParagraphStyle(name="CimsRight", parent=styles["CimsSmall"], alignment=TA_RIGHT))
    styles.add(
        ParagraphStyle(
            name="CimsTableHead",
            parent=styles["CimsSmall"],
            fontName="Helvetica-Bold",
            textColor=colors.white,
        )
    )

    def p(value):
        return Paragraph(escape(str(value or "")), styles["CimsSmall"])

    story = [
        Paragraph("PURCHASE ORDER", styles["CimsTitle"]),
        Paragraph(escape(order["po_no"]), styles["CimsSmall"]),
        Spacer(1, 20),
    ]
    meta = Table(
        [
            [p("SUPPLIER"), p("DELIVER TO")],
            [p(vendor["name"]), p(location["name"])],
            [
                p(vendor.get("gstin") or vendor.get("email") or ""),
                p(location.get("address_line1") or location["code"]),
            ],
            [
                p(f"Order date: {order['order_date']:%d %b %Y}"),
                p(f"Expected: {order['expected_date']:%d %b %Y}" if order["expected_date"] else ""),
            ],
        ],
        colWidths=[255, 255],
    )
    meta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SILVER),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.extend([meta, Spacer(1, 20)])
    rows = [
        [
            Paragraph(label, styles["CimsTableHead"])
            for label in ("#", "ITEM", "QTY", "PRICE", "TAX", "TOTAL")
        ]
    ]
    for line in order["lines"]:
        item = items[line["item_id"]]
        rows.append(
            [
                p(line["line_no"]),
                p(f"{item['sku']}  {item['name']}"),
                p(line["quantity"]),
                p(f"{line['unit_price']:,.2f}"),
                p(f"{line['tax_amount']:,.2f}"),
                p(f"{line['line_total']:,.2f}"),
            ]
        )
    table = Table(rows, colWidths=[28, 222, 55, 68, 63, 74], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), AUBERGINE),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SILVER]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend([table, Spacer(1, 16)])
    totals = Table(
        [
            [p("Subtotal"), Paragraph(f"INR {order['subtotal']:,.2f}", styles["CimsRight"])],
            [p("Discount"), Paragraph(f"INR {order['discount_total']:,.2f}", styles["CimsRight"])],
            [p("Tax"), Paragraph(f"INR {order['tax_total']:,.2f}", styles["CimsRight"])],
            [p("TOTAL"), Paragraph(f"INR {order['grand_total']:,.2f}", styles["CimsRight"])],
        ],
        colWidths=[110, 130],
        hAlign="RIGHT",
    )
    totals.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 3), (-1, 3), 1.5, MARIGOLD),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(totals)
    if order.get("notes"):
        story.extend([Spacer(1, 18), p("NOTES"), p(order["notes"])])
    document.build(story)
    return output.getvalue()
