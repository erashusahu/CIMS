"""Packing slip for a reviewed or shipped sales shipment."""

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import catalog as catalog_db


async def packing_slip_pdf(service, shipment, order) -> bytes:
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=(210 * mm, 297 * mm),
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Packing slip", styles["Title"]),
        Paragraph(f"Shipment {escape(shipment['shipment_no'])}", styles["Heading2"]),
        Paragraph(f"Sales order {escape(order['order_no'])}", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]
    if shipment["carrier"] or shipment["tracking_no"]:
        story.append(
            Paragraph(
                f"Carrier: {escape(shipment['carrier'] or '-')} | "
                f"Tracking: {escape(shipment['tracking_no'] or '-')}",
                styles["Normal"],
            )
        )
        story.append(Spacer(1, 5 * mm))
    for package in shipment["packages"]:
        story.append(Paragraph(f"Package {escape(package['package_no'])}", styles["Heading3"]))
        dimensions = [package[key] for key in ("length_cm", "width_cm", "height_cm")]
        details = []
        if package["weight_kg"] is not None:
            details.append(f"Weight: {package['weight_kg']} kg")
        if all(value is not None for value in dimensions):
            details.append(f"Dimensions: {' x '.join(map(str, dimensions))} cm")
        if details:
            story.append(Paragraph(escape(" | ".join(details)), styles["Normal"]))
        rows = [["Item", "SKU", "Quantity"]]
        for content in package["contents"]:
            item = await service.catalog.row(catalog_db.items, "item_id", content["item_id"])
            rows.append(
                [
                    Paragraph(escape(item["name"]), styles["BodyText"]),
                    escape(item["sku"]),
                    str(content["quantity"]),
                ]
            )
        table = Table(rows, colWidths=[90 * mm, 50 * mm, 32 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#33213e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d6dce2")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.extend([Spacer(1, 3 * mm), table, Spacer(1, 8 * mm)])
    document.build(story)
    return output.getvalue()
