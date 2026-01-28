from django.contrib import admin
from .models import Client, Subscription, ClientEmail, WorkLog
from django.utils.html import format_html
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from .models import Invoice, InvoiceLine
from django.conf import settings
from django.core.mail import EmailMessage

from billing.services.pdf import generate_invoice_pdf

def ensure_invoice_pdf(invoice):
    """Ensure invoice.pdf is generated and saved.

    Uses the same PDF generator as monthly invoices: billing.services.pdf.generate_invoice_pdf.
    The generator returns a file-like object; we persist it into the Invoice.pdf FileField.
    """
    # If PDF already exists, nothing to do
    if getattr(invoice, "pdf", None) is not None and invoice.pdf:
        return

    pdf_file = generate_invoice_pdf(invoice)
    # Persist into FileField so it is available for downloads and email attachments
    invoice.pdf.save(pdf_file.name, pdf_file, save=True)

class ClientEmailInline(admin.TabularInline):
    model = ClientEmail
    extra = 1


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "company_code", "email", "active")
    search_fields = ("name", "company_code", "vat_code")
    list_filter = ("active",)
    inlines = [ClientEmailInline]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("client", "title", "monthly_fee", "hosting_yearly_fee", "hosting_valid_until", "active")
    list_filter = ("active",)
    search_fields = ("client__name", "title")

@admin.register(WorkLog)
class WorkLogAdmin(admin.ModelAdmin):
    list_display = ("client", "date", "description", "quantity", "unit_price", "total", "billed")
    list_filter = ("client", "billed", "date")
    search_fields = ("description",)
    date_hierarchy = "date"
    actions = ["generate_invoice_for_selected"]

    def total(self, obj):
        return obj.total_price()
    total.short_description = "Suma (€)"

    @admin.action(description="Išrašyti sąskaitą pažymėtiems darbams")
    def generate_invoice_for_selected(self, request, queryset):
        queryset = queryset.select_related("client").filter(billed=False)

        if not queryset.exists():
            self.message_user(
                request,
                "Nėra neapmokėtų (nebilled) darbų.",
                level=messages.WARNING
            )
            return

        today = timezone.localdate()

        # Grupavimas pagal klientą
        by_client = {}
        for wl in queryset:
            by_client.setdefault(wl.client_id, []).append(wl)

        created = 0

        for client_id, logs in by_client.items():
            client = logs[0].client

            with transaction.atomic():
                net = sum([wl.total_price() for wl in logs], Decimal("0.00"))
                vat_rate = Decimal("0.21")
                vat = (net * vat_rate).quantize(Decimal("0.01"))
                total = (net + vat).quantize(Decimal("0.01"))

                # Numeracija: MEV23-001, MEV23-002 ...
                prefix = "MEV26"

                last_invoice = (
                    Invoice.objects.filter(number__startswith=f"{prefix}-")
                    .order_by("-number")
                    .first()
                )

                if not last_invoice:
                    number = f"{prefix}-001"
                else:
                    last_seq = int(last_invoice.number.split("-")[1])
                    number = f"{prefix}-{last_seq + 1:03d}"

                invoice = Invoice.objects.create(
                    number=number,
                    client=client,
                    invoice_type="manual",
                    period_from=min(wl.date for wl in logs),
                    period_to=max(wl.date for wl in logs),
                    issued_date=today,
                    due_date=today + timezone.timedelta(days=10),
                    net_amount=net,
                    vat_rate=vat_rate,
                    vat_amount=vat,
                    total_amount=total,
                )

                for wl in logs:
                    line_total = wl.total_price().quantize(Decimal("0.01"))
                    InvoiceLine.objects.create(
                        invoice=invoice,
                        description=wl.description,
                        quantity=wl.quantity,
                        unit_price=wl.unit_price,
                        total=line_total,
                    )

                WorkLog.objects.filter(
                    id__in=[wl.id for wl in logs]
                ).update(billed=True)

                # --- Išsiųsti el. paštu (kaip mėnesinėse sąskaitose) ---
                # Surenkam gavėjus: pagrindinis kliento email + papildomi iš ClientEmail
                recipients = []
                if getattr(client, "email", None):
                    recipients.append(client.email)

                extra_emails = list(
                    ClientEmail.objects.filter(client=client)
                    .values_list("email", flat=True)
                )
                for e in extra_emails:
                    if e and e not in recipients:
                        recipients.append(e)

                # Jei nėra gavėjų – tik pranešam admin'e ir praleidžiam siuntimą
                if not recipients:
                    self.message_user(
                        request,
                        f"Klientui '{client.name}' nėra nurodyto el. pašto – sąskaita sukurta, bet neišsiųsta.",
                        level=messages.WARNING,
                    )
                else:
                    # Sugeneruojam PDF (naudojam esamą projekto generatorių, jei yra)
                    ensure_invoice_pdf(invoice)

                    subject = f"Sąskaita {invoice.number}"
                    body = (
                        f"Sveiki,\n\n"
                        f"Prisegta sąskaita {invoice.number}.\n"
                        f"Suma: {invoice.total_amount} EUR\n"
                        f"Apmokėti iki: {invoice.due_date}\n\n"
                        f"Pagarbiai,\n"
                        f"{getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@localhost')}\n"
                    )

                    msg = EmailMessage(
                        subject=subject,
                        body=body,
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                        to=recipients,
                    )

                    # Prisegam PDF jei yra (saugesnis variantas nei invoice.pdf.path)
                    if getattr(invoice, "pdf", None) is not None and invoice.pdf:
                        with invoice.pdf.open("rb") as f:
                            msg.attach(f"{invoice.number}.pdf", f.read(), "application/pdf")

                    msg.send(fail_silently=False)

                created += 1

        self.message_user(
            request,
            f"Sukurta sąskaitų: {created}",
            level=messages.SUCCESS
        )



class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "client",
        "invoice_type",
        "issued_date",
        "total_amount",
        "paid",
        "pdf_link",
    )
    list_filter = ("invoice_type", "paid", "issued_date")
    search_fields = ("number", "client__name")
    inlines = [InvoiceLineInline]

    def pdf_link(self, obj):
        if obj.pdf:
            return format_html('<a href="{}" target="_blank">PDF</a>', obj.pdf.url)
        return "-"

    pdf_link.short_description = "PDF"