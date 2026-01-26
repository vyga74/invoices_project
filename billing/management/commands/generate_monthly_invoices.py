from datetime import date
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing.models import Client, Invoice, InvoiceLine, WorkLog


class Command(BaseCommand):
    help = "Generuoja mėnesines sąskaitas visiems aktyviems klientams"

    def handle(self, *args, **options):
        today = timezone.now().date()
        year = today.year
        month = today.month

        period_from = date(year, month, 1)
        if month == 12:
            period_to = date(year, 12, 31)
        else:
            period_to = date(year, month + 1, 1) - timezone.timedelta(days=1)

        for client in Client.objects.filter(active=True):
            self.generate_for_client(client, period_from, period_to)

        self.stdout.write(self.style.SUCCESS("Sąskaitų generavimas baigtas ✅"))

    @transaction.atomic
    def generate_for_client(self, client, period_from, period_to):
        subscription = getattr(client, "subscription", None)
        if not subscription or not subscription.active:
            return

        work_logs = WorkLog.objects.filter(
            client=client,
            billed=False,
            date__range=(period_from, period_to),
        )

        if not work_logs.exists():
            return

        invoice_number = self.generate_invoice_number()

        invoice = Invoice.objects.create(
            number=invoice_number,
            client=client,
            invoice_type="monthly",
            period_from=period_from,
            period_to=period_to,
            issued_date=timezone.now().date(),
            due_date=timezone.now().date() + timezone.timedelta(days=14),
            total_amount=0,
        )

        total = 0

        # Abonementas
        InvoiceLine.objects.create(
            invoice=invoice,
            description="Mėnesinis abonementas",
            quantity=1,
            unit_price=subscription.monthly_fee,
            total=subscription.monthly_fee,
        )
        total += subscription.monthly_fee

        # Papildomi darbai
        for work in work_logs:
            line_total = work.total_price()
            InvoiceLine.objects.create(
                invoice=invoice,
                description=work.description,
                quantity=work.quantity,
                unit_price=work.unit_price,
                total=line_total,
            )
            total += line_total
            work.billed = True
            work.save(update_fields=["billed"])

        invoice.total_amount = total
        invoice.save(update_fields=["total_amount"])

        self.stdout.write(f"Sukurta sąskaita {invoice.number} klientui {client.name}")

    def generate_invoice_number(self):
        today = timezone.now().date()
        prefix = today.strftime("%Y%m")

        last_invoice = (
            Invoice.objects.filter(number__startswith=prefix)
            .order_by("-number")
            .first()
        )

        if not last_invoice:
            return f"{prefix}-001"

        last_seq = int(last_invoice.number.split("-")[1])
        return f"{prefix}-{last_seq + 1:03d}"