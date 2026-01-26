from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from billing.models import Invoice
from billing.services.pdf import generate_invoice_pdf


class Command(BaseCommand):
    help = "Sugeneruoja PDF pasirinktai sąskaitai (pagal numerį) arba visoms be PDF"

    def add_arguments(self, parser):
        parser.add_argument("--number", type=str, help="Sąskaitos numeris (pvz. 202601-001)")

    @transaction.atomic
    def handle(self, *args, **options):
        number = options.get("number")

        if number:
            invoices = Invoice.objects.filter(number=number)
            if not invoices.exists():
                raise CommandError(f"Nerasta sąskaita su numeriu: {number}")
        else:
            invoices = Invoice.objects.filter(pdf__isnull=True)

        count = 0
        for inv in invoices:
            pdf_file = generate_invoice_pdf(inv)
            inv.pdf.save(pdf_file.name, pdf_file, save=True)
            count += 1

        self.stdout.write(self.style.SUCCESS(f"PDF sugeneruota: {count}"))