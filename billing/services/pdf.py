import os
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from django.conf import settings
from pathlib import Path
from io import BytesIO
from decimal import Decimal

from django.core.files.base import ContentFile
from django.utils import timezone

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

FONT_PATH = Path(settings.BASE_DIR) / "billing" / "assets" / "fonts" / "DejaVuSans.ttf"
FONT_PATH2 = Path(settings.BASE_DIR) / "billing" / "assets" / "fonts" / "DejaVuSans-Bold.ttf"

LOGO_PATH = Path(settings.BASE_DIR) / "billing" / "assets" / "logo.png"

pdfmetrics.registerFont(TTFont("DejaVu", FONT_PATH))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", FONT_PATH2))

def amount_to_words_lt(amount):
    from decimal import Decimal
    try:
        from num2words import num2words

        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
        euros = int(amt)
        cents = int((amt - euros) * 100)

        words = num2words(euros, lang="lt")

        # Lithuanian declension rules for "euras"
        last_two = euros % 100
        last_one = euros % 10

        if last_two in (11, 12, 13, 14):
            euro_word = "eurų"
        elif last_one == 1:
            euro_word = "euras"
        elif last_one in (2, 3, 4):
            euro_word = "eurai"
        else:
            euro_word = "eurų"

        if cents > 0:
            return f"{words} {euro_word} {cents:02d} ct"
        return f"{words} {euro_word}"

    except Exception:
        amt = Decimal(str(amount)).quantize(Decimal("0.01"))
        return f"{amt:.2f} EUR"

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
    # Logo (top-right). Put your logo file at: billing/assets/logo.png
    # If the file doesn't exist, PDF will still be generated without a logo.
    try:
        if LOGO_PATH.exists():
            img = ImageReader(str(LOGO_PATH))
            logo_w = 110
            logo_h = 40
            c.drawImage(
                img,
                width - 40 - logo_w,
                height - 50 - logo_h + 10,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask='auto',
            )
    except Exception:
        pass

    is_proforma = (getattr(invoice, "invoice_type", "") == "hosting")
    doc_title = "IŠANKSTINĖ SĄSKAITA" if is_proforma else "PVM SĄSKAITA FAKTŪRA"
    show_number = not is_proforma

    c.setFont("DejaVu-Bold", 16)
    c.drawString(x, y, f"{doc_title}")
    y -= 25

    c.setFont("DejaVu", 10)
    if show_number:
        c.drawString(x, y, f"Nr: {invoice.number}")
    else:   
        c.drawString(x, y, "")
    y -= 15
    c.drawString(x, y, f"Išrašymo data: {invoice.issued_date}")
    y -= 15
    c.drawString(x, y, f"Apmokėti iki: {invoice.due_date}")
    y -= 25

    # Seller + Client (side-by-side)
    left_x = x
    right_x = x + 280
    block_top_y = y

    # Titles
    c.setFont("DejaVu-Bold", 11)
    c.drawString(left_x, block_top_y, "Tiekėjas:")
    c.drawString(right_x, block_top_y, "Pirkėjas:")

    # Seller lines (left column)
    seller_y = block_top_y - 14
    c.setFont("DejaVu", 10)
    seller_lines = [
        "MEVIKA UAB",
        "Įmonės kodas: 302666445",
        "PVM kodas: LT100009187014",
        "A/S: LT114010044200904314",
        "Adresas: Darbo g. 19, Kuršėnai",
    ]
    for s in seller_lines:
        c.drawString(left_x, seller_y, s)
        seller_y -= 14

    # Client lines (right column)
    client_y = block_top_y - 14
    c.setFont("DejaVu", 10)
    client_lines = [invoice.client.name]

    if getattr(invoice.client, "company_code", ""):
        client_lines.append(f"Įmonės kodas: {invoice.client.company_code}")

    if getattr(invoice.client, "vat_code", ""):
        client_lines.append(f"PVM kodas: {invoice.client.vat_code}")

    if getattr(invoice.client, "address", ""):
        # trumpai, kad neišvažiuotų į šoną
        addr = (invoice.client.address or "").replace("\n", ", ")
        client_lines.append(f"Adresas: {addr[:120]}")

    for s in client_lines:
        c.drawString(right_x, client_y, s)
        client_y -= 14

    # Move y to below the taller of the two columns
    y = min(seller_y, client_y) - 10


    # Table header
    c.setFont("DejaVu-Bold", 10)
    c.drawString(x, y, "Aprašymas")
    c.drawString(x + 330, y, "Kiekis")
    c.drawString(x + 400, y, "Kaina")
    c.drawString(x + 470, y, "Suma")
    y -= 10
    c.line(x, y, width - 40, y)
    y -= 15

    # Lines
    c.setFont("DejaVu", 10)

    # 1) Lentelėje rodome tik realias paslaugų/prekių eilutes (ne PVM eilutę, jei ji buvo sukurta kaip InvoiceLine)
    lines_qs = invoice.lines.all()
    table_total = Decimal("0.00")

    for line in lines_qs:
        # Jei kažkur anksčiau PVM buvo sukurtas kaip atskira eilutė – jos nerodom lentelėje
        desc_raw = (line.description or "").strip()
        if desc_raw.lower().startswith("pvm") or desc_raw.lower() in {"vat", "vat 21%", "pvm 21%"}:
            continue

        desc = desc_raw
        if len(desc) > 55:
            desc = desc[:52] + "..."

        c.drawString(x, y, desc)
        c.drawRightString(x + 370, y, f"{line.quantity}")
        c.drawRightString(x + 450, y, f"{Decimal(str(line.unit_price)).quantize(Decimal('0.01')):.2f}")
        c.drawRightString(x + 530, y, f"{Decimal(str(line.total)).quantize(Decimal('0.01')):.2f}")

        table_total += Decimal(str(line.total or 0))

        y -= 14
        if y < 120:
            c.showPage()
            y = height - 60
            c.setFont("DejaVu", 10)

    # 2) Skaičiuojam sumas (be PVM, PVM, su PVM)
    # Jei modelyje jau turi laukus (invoice.net_amount / invoice.vat_amount / invoice.vat_rate / invoice.total_amount) – naudojam juos.
    net_amount = getattr(invoice, "net_amount", None)
    vat_amount = getattr(invoice, "vat_amount", None)
    vat_rate = getattr(invoice, "vat_rate", None)
    gross_amount = getattr(invoice, "total_amount", None)

    if net_amount is None:
        net_amount = table_total
    else:
        net_amount = Decimal(str(net_amount))

    if vat_rate is None:
        vat_rate = Decimal("0.21")
    else:
        vat_rate = Decimal(str(vat_rate))

    if vat_amount is None:
        vat_amount = (net_amount * vat_rate).quantize(Decimal("0.01"))
    else:
        vat_amount = Decimal(str(vat_amount))

    if gross_amount is None:
        gross_amount = (net_amount + vat_amount).quantize(Decimal("0.01"))
    else:
        gross_amount = Decimal(str(gross_amount))

    # 3) Totals blokas apačioje
    y -= 10
    c.line(x, y, width - 40, y)
    y -= 18

    c.setFont("DejaVu-Bold", 10)
    c.drawRightString(width - 40, y, f"Suma be PVM: {net_amount:.2f} €")
    y -= 14

    c.setFont("DejaVu", 10)
    c.drawRightString(width - 40, y, f"PVM ({(vat_rate * Decimal('100')).quantize(Decimal('0')):.0f}%): {vat_amount:.2f} €")
    y -= 16

    c.setFont("DejaVu-Bold", 12)
    c.drawRightString(width - 40, y, f"Iš viso su PVM: {gross_amount:.2f} €")

    # 4) Suma žodžiais
    y -= 22
    c.setFont("DejaVu", 10)
    c.drawString(x, y, f"Suma žodžiais: {amount_to_words_lt(gross_amount)}")

    # 4) Suma žodžiais
    y -= 22
    c.setFont("DejaVu", 10)
    c.drawString(x, y, f"Išrašė: Direktorius Vygandas Milieška")


    # Footer
    y -= 35
    c.setFont("DejaVu", 9)
    c.drawString(x, 40, f"Sugeneruota: {timezone.now().strftime('%Y-%m-%d %H:%M')}")

    c.showPage()
    c.save()

    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"{invoice.number}.pdf"
    return ContentFile(pdf_bytes, name=filename)