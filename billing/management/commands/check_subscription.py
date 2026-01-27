from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.utils import timezone

# Pernaudojam jau turimą logiką iš mėnesinių sąskaitų komandos
from billing.management.commands.generate_monthly_invoices import (
    Command as MonthlyInvoicesCommand,
)

from billing.models import Invoice, InvoiceLine, Subscription


class Command(MonthlyInvoicesCommand):
    """
    Tikrinam metines (hosting) prenumeratas ir, jei reikia, išrašom išankstinę sąskaitą.

    Logika:
    - Jei hosting_yearly_fee > 0 ir hosting_valid_until nustatyta, skaičiuojam kiek dienų liko.
    - Jei liko <= 30 d. ir dar nėra išrašyta išankstinė sąskaita šiam galiojimo laikotarpiui,
      sukuriam sąskaitą (invoice_type='hosting') ir išsiunčiam el. paštu.
    - Jei liko <= 10 d. ir sąskaita egzistuoja, bet dar neapmokėta – išsiunčiam priminimą (tas pats PDF).
    """

    help = (
        "Patikrina metinius hosting abonementus. "
        "Jei iki pabaigos liko <=30 d. – sukuria išankstinę sąskaitą ir išsiunčia el. paštu. "
        "Jei liko <=10 d. ir sąskaita dar neapmokėta – išsiunčia priminimą."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--today",
            type=str,
            help="(Testams) Nurodyti šiandienos datą YYYY-MM-DD. Jei nenurodyta – naudos serverio datą.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Kiek dienų prieš pabaigą generuoti išankstinę sąskaitą (default: 30).",
        )
        parser.add_argument(
            "--remind-days",
            type=int,
            default=10,
            help="Kiek dienų prieš pabaigą siųsti priminimą, jei neapmokėta (default: 10).",
        )

    def handle(self, *args, **options):
        today_opt = options.get("today")
        if today_opt:
            today = date.fromisoformat(today_opt)
        else:
            today = timezone.now().date()

        days = int(options.get("days") or 30)
        remind_days = int(options.get("remind_days") or 10)

        self.check_annual_subscriptions(today=today, days=days, remind_days=remind_days)
        self.stdout.write(self.style.SUCCESS("Metinių abonementų patikra baigta ✅"))

    # -------------------------
    # Core logic
    # -------------------------

    def check_annual_subscriptions(self, today: date, days: int = 30, remind_days: int = 10) -> None:
        qs = (
            Subscription.objects.select_related("client")
            .filter(active=True)
            .exclude(hosting_valid_until__isnull=True)
            .exclude(hosting_yearly_fee__isnull=True)
        )

        for sub in qs:
            # 0 arba mažiau – praleidžiam
            yearly_fee = Decimal(str(sub.hosting_yearly_fee or 0))
            if yearly_fee <= 0:
                continue

            valid_until = sub.hosting_valid_until
            days_left = (valid_until - today).days

            # jei jau pasibaigęs – nieko nerašom (galima vėliau daryti "overdue" logiką)
            if days_left < 0:
                continue

            # 1) Išankstinė sąskaita likus <= days
            if days_left <= days:
                invoice = self._get_hosting_invoice_for_period(sub=sub, period_to=valid_until)

                if invoice is None:
                    invoice = self._create_hosting_invoice(sub=sub, issued_date=today)
                    self._ensure_pdf_and_email(invoice, is_reminder=False)
                    self.stdout.write(
                        f"🧾 Hosting išankstinė sąskaita sukurta: {invoice.number} (liko {days_left} d.) → {sub.client.name}"
                    )
                else:
                    # 2) Priminimas likus <= remind_days, jei neapmokėta
                    if days_left <= remind_days and not bool(getattr(invoice, "paid", False)):
                        self._ensure_pdf_and_email(invoice, is_reminder=True)
                        self.stdout.write(
                            f"🔔 Hosting priminimas išsiųstas: {invoice.number} (liko {days_left} d.) → {sub.client.name}"
                        )

    # -------------------------
    # Helpers
    # -------------------------

    def _get_hosting_invoice_for_period(self, sub: Subscription, period_to: date) -> Invoice | None:
        """
        Ieškom jau sugeneruotos hosting sąskaitos šiam galiojimo terminui.
        Tam naudojam invoice_type='hosting' ir period_to = hosting_valid_until.
        """
        return (
            Invoice.objects.filter(
                client=sub.client,
                invoice_type="hosting",
                period_to=period_to,
            )
            .order_by("-issued_date", "-id")
            .first()
        )

    def _create_hosting_invoice(self, sub: Subscription, issued_date: date) -> Invoice:
        """
        Sukuria invoice + 1 eilutę už hosting metams (be PVM/PVM/total logiką paliekam tavo esamai
        mėnesinei komandai – jei ji turi net_amount/vat_amount/total_amount laukus, užpildysim čia.
        """
        invoice_number = self.generate_invoice_number()

        # Jei tavo Invoice modelyje yra net_amount/vat_amount/vat_rate – užpildom.
        # Jei dar nėra – Django tiesiog ignoruos šiuos kwargs? (neignoruoja). Todėl dedam saugiai per setattr po create.
        invoice = Invoice.objects.create(
            number=invoice_number,
            client=sub.client,
            invoice_type="hosting",
            period_from=issued_date,  # paprastai išrašymo data
            period_to=sub.hosting_valid_until,
            issued_date=issued_date,
            due_date=issued_date + timezone.timedelta(days=14),
            total_amount=Decimal("0.00"),
        )

        yearly_fee = Decimal(str(sub.hosting_yearly_fee))
        InvoiceLine.objects.create(
            invoice=invoice,
            description=f"Hosting pratęsimas iki {sub.hosting_valid_until}",
            quantity=1,
            unit_price=yearly_fee,
            total=yearly_fee,
        )

        # Suskaičiuojam sumas (be PVM / PVM / su PVM), jei tavo Invoice turi atitinkamus laukus
        vat_rate = Decimal("0.21")
        net_amount = yearly_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat_amount = (net_amount * vat_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross_amount = (net_amount + vat_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # total_amount visada turim
        invoice.total_amount = gross_amount

        # Optional fields (jei egzistuoja)
        if hasattr(invoice, "net_amount"):
            setattr(invoice, "net_amount", net_amount)
        if hasattr(invoice, "vat_amount"):
            setattr(invoice, "vat_amount", vat_amount)
        if hasattr(invoice, "vat_rate"):
            setattr(invoice, "vat_rate", vat_rate)

        invoice.save()
        return invoice

    def _ensure_pdf_and_email(self, invoice: Invoice, is_reminder: bool) -> None:
        """
        1) Sukuriam PDF (jei tavo mėnesinė komanda turi tam metodą)
        2) Išsiunčiam el. laišką (jei turi tam metodą)
        """
        # 1) PDF
        # Dažniausi metodų pavadinimai – bandome kelis.
        for pdf_method_name in ("generate_pdf_for_invoice", "generate_invoice_pdf", "create_invoice_pdf"):
            pdf_method = getattr(self, pdf_method_name, None)
            if callable(pdf_method):
                pdf_method(invoice)
                break

        # 2) Email
        # Jei mėnesinėje komandoje turi funkciją siųsti – panaudojam. Kitu atveju paliekam tik PDF sugeneravimą.
        email_method = getattr(self, "send_invoice_email", None)
        if callable(email_method):
            if is_reminder:
                email_method(invoice, subject_prefix="PRIMINIMAS: ")
            else:
                email_method(invoice)
