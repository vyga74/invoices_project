import os
from io import BytesIO
from decimal import Decimal

from django.core.files.base import ContentFile
from django.utils import timezone

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def generate_invoice_pdf(invoice) -> ContentFile:
    """
    Sugeneruoja PDF į memory ir grąžina ContentFile, kurį galima priskirti invoice.pdf.save(...)
    """
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    width, height = A4
    x = 40
    y = height - 50

    # Header
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, "SĄSKAITA-FAKTŪRA")
    y -= 25

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Nr: {invoice.number}")
    y -= 15
    c.drawString(x, y, f"Išrašymo data: {invoice.issued_date}")
    y -= 15
    c.drawString(x, y, f"Apmokėti iki: {invoice.due_date}")
    y -= 25

    # Client
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, "Pirkėjas:")
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(x, y, invoice.client.name)
    y -= 14
    if getattr(invoice.client, "company_code", ""):
        c.drawString(x, y, f"Įmonės kodas: {invoice.client.company_code}")
        y -= 14
    if getattr(invoice.client, "vat_code", ""):
        c.drawString(x, y, f"PVM kodas: {invoice.client.vat_code}")
        y -= 14
    if getattr(invoice.client, "address", ""):
        # trumpai, kad neišvažiuotų į šoną
        addr = (invoice.client.address or "").replace("\n", ", ")
        c.drawString(x, y, f"Adresas: {addr[:120]}")
        y -= 20

    # Table header
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, "Aprašymas")
    c.drawString(x + 330, y, "Kiekis")
    c.drawString(x + 400, y, "Kaina")
    c.drawString(x + 470, y, "Suma")
    y -= 10
    c.line(x, y, width - 40, y)
    y -= 15

    # Lines
    c.setFont("Helvetica", 10)
    total = Decimal("0.00")

    for line in invoice.lines.all():
        desc = line.description
        if len(desc) > 55:
            desc = desc[:52] + "..."

        c.drawString(x, y, desc)
        c.drawRightString(x + 370, y, f"{line.quantity}")
        c.drawRightString(x + 450, y, f"{line.unit_price:.2f}")
        c.drawRightString(x + 530, y, f"{line.total:.2f}")
        total += Decimal(str(line.total))

        y -= 14
        if y < 80:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica", 10)

    # Total
    y -= 10
    c.line(x, y, width - 40, y)
    y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(width - 40, y, f"Iš viso: {total:.2f} €")

    # Footer
    y -= 35
    c.setFont("Helvetica", 9)
    c.drawString(x, 40, f"Sugeneruota: {timezone.now().strftime('%Y-%m-%d %H:%M')}")

    c.showPage()
    c.save()

    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"{invoice.number}.pdf"
    return ContentFile(pdf_bytes, name=filename)