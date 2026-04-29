from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing.models import Client, Invoice, InvoiceLine, WorkLog
from django.conf import settings
from django.core.mail import EmailMessage
from billing.services.pdf import generate_invoice_pdf


class Command(BaseCommand):
    help = "Generuoja mėnesines sąskaitas visiems aktyviams klientams (su PVM 21%)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Jei sąskaita už tą patį laikotarpį jau yra – ištrinti ir pergeneruoti iš naujo.",
        )
        parser.add_argument(
            "--resend",
            action="store_true",
            help="Jei sąskaita už tą patį laikotarpį jau yra – neskaičiuoti iš naujo, tik persiųsti el. paštu (su PDF).",
        )
        parser.add_argument(
            "--month",
            type=str,
            help="Generuoti konkrečiam mėnesiui formatu YYYY-MM (pvz. 2026-01). Jei nenurodyta ir šiandien yra mėnesio 1 d. – generuos už praeitą mėnesį.",
        )
        parser.add_argument(
            "--issued-date",
            type=str,
            help="Nurodyti sąskaitos išrašymo datą (YYYY-MM-DD). Jei nenurodyta ir šiandien yra mėnesio 1 d. – naudos vakarykštę datą.",
        )

    def handle(self, *args, **options):
        today = timezone.now().date()

        # Default elgsena:
        # - Jei paleidžiama mėnesio 1 d. ir neperduotas --month, generuojam už praeitą mėnesį.
        # - Jei paleidžiama bet kurią kitą dieną, generuojam už einamą mėnesį.
        month_opt = options.get("month")
        if month_opt:
            y_str, m_str = month_opt.split("-")
            year = int(y_str)
            month = int(m_str)
        else:
            if today.day == 1:
                prev_day = today - timezone.timedelta(days=1)
                year = prev_day.year
                month = prev_day.month
            else:
                year = today.year
                month = today.month

        # Išrašymo data:
        issued_date_opt = options.get("issued_date")
        if issued_date_opt:
            issued_date = date.fromisoformat(issued_date_opt)
        else:
            issued_date = today - timezone.timedelta(days=1) if today.day == 1 else today

        period_from = date(year, month, 1)
        if month == 12:
            period_to = date(year, 12, 31)
        else:
            period_to = date(year, month + 1, 1) - timezone.timedelta(days=1)

        for client in Client.objects.filter(active=True):
            self.generate_for_client(
                client,
                period_from,
                period_to,
                issued_date,
                force=options.get("force", False),
                resend=options.get("resend", False),
            )

        self.stdout.write(self.style.SUCCESS("Sąskaitų generavimas baigtas ✅"))

    def send_invoice_email(self, invoice: Invoice) -> None:
        client = invoice.client

        # --- TEST REŽIMAS ---
        SEND_ONLY_TO_ADMIN = True
        ADMIN_EMAIL = getattr(settings, "ADMIN_INVOICE_EMAIL", "vyga@infsis.lt")
        # --------------------

        recipients = []

        # 1) Paimam papildomus klientų el. paštus (ClientEmail)
        emails_mgr = getattr(client, "emails", None)  # jei related_name="emails"
        if emails_mgr is None:
            emails_mgr = getattr(client, "clientemail_set", None)  # default related_name

        if emails_mgr is not None:
            try:
                recipients = list(emails_mgr.values_list("email", flat=True))
            except Exception:
                recipients = []

        # 2) Jei nėra – naudojam pagrindinį client.email
        if not recipients:
            main_email = getattr(client, "email", "")
            if main_email:
                recipients = [main_email]

        if SEND_ONLY_TO_ADMIN:
            original_recipients = recipients.copy()
            recipients = [ADMIN_EMAIL]
            self.stdout.write(
                self.style.WARNING(
                    f"TEST režimas: klientui {client.name} laiškas nesiunčiamas. Vietoje to siunčiama tik į {ADMIN_EMAIL}. Originalūs gavėjai: {', '.join(original_recipients) if original_recipients else 'nėra'}"
                )
            )

        if not recipients:
            self.stdout.write(self.style.WARNING(f"⚠️ Klientas {client.name} neturi el. pašto – nesiunčiu."))
            return

        # Užtikrinam, kad PDF yra
        if getattr(invoice, "pdf", None) is not None and not invoice.pdf:
            pdf_file = generate_invoice_pdf(invoice)
            invoice.pdf.save(pdf_file.name, pdf_file, save=True)

        is_proforma = invoice.invoice_type == "hosting"

        subject = (
            f"Išankstinė sąskaita – {client.name}"
            if is_proforma
            else f"PVM sąskaita faktūra {invoice.number} – {client.name}"
        )

        body = (
            "Sveiki,\n\n"
            f"Prisegame sąskaitą {invoice.number} už laikotarpį {invoice.period_from} – {invoice.period_to}.\n\n"
            "Geros dienos.\n"
        )

        admin_copy_email = getattr(settings, "INVOICE_ADMIN_COPY_EMAIL", "").strip() or "info@mevika.lt"

        msg = EmailMessage(
            subject=subject,
            body=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
            to=recipients,
        )

        if getattr(invoice, "pdf", None) is not None and invoice.pdf:
            with invoice.pdf.open("rb") as f:
                msg.attach(f"{invoice.number}.pdf", f.read(), "application/pdf")
        
        self.stdout.write(f"DEBUG recipients: {msg.recipients()}")

        # Pagrindinis laiškas klientui
        msg.send(fail_silently=False)
        self.stdout.write(self.style.SUCCESS(f"📧 Išsiųsta: {invoice.number} → {', '.join(recipients)}"))

        # Patikimas admin kopijos siuntimas (ne BCC), nes kai kurie SMTP nepristato BCC gavėjų
        if admin_copy_email:
            copy_msg = EmailMessage(
                subject=f"[KOPIJA] {subject}",
                body=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
                to=[admin_copy_email],
            )

            if getattr(invoice, "pdf", None) is not None and invoice.pdf:
                with invoice.pdf.open("rb") as f:
                    copy_msg.attach(f"{invoice.number}.pdf", f.read(), "application/pdf")

            copy_msg.send(fail_silently=False)
            self.stdout.write(self.style.SUCCESS(f"📧 Kopija išsiųsta → {admin_copy_email}"))

    @transaction.atomic
    def generate_for_client(
        self,
        client,
        period_from,
        period_to,
        issued_date,
        *,
        force: bool = False,
        resend: bool = False,
    ):
        # Imame tik aktyvius abonementus. Nuliniai (0.00) mėnesiniai abonementai bus praleisti žemiau.
        existing = (
            Invoice.objects.filter(
                client=client,
                invoice_type="monthly",
                period_from=period_from,
                period_to=period_to,
            )
            .order_by("-id")
            .first()
        )

        # Jei sąskaita už šį laikotarpį jau sukurta – pagal režimą arba persiunčiam, arba pergeneruojam, arba praleidžiam.
        if existing and resend and not force:
            self.send_invoice_email(existing)
            self.stdout.write(
                self.style.WARNING(
                    f"↩️ Sąskaita {existing.number} už {period_from}–{period_to} jau yra – persiųsta el. paštu."
                )
            )
            return

        if existing and force:
            existing.delete()
            existing = None

        if existing and not force:
            self.stdout.write(
                self.style.WARNING(
                    f"⏭️ Sąskaita už {period_from}–{period_to} klientui {client.name} jau yra ({existing.number}) – praleidžiu."
                )
            )
            return

        subscriptions = client.subscriptions.filter(active=True)

        work_logs = WorkLog.objects.filter(
            client=client,
            billed=False,
            date__range=(period_from, period_to),
        )

        # Pastaba: sąskaitą generuojame ir tada, kai nėra papildomų darbų (work_logs tuščias).
        # Tokiu atveju sąskaitoje bus tik abonementų eilutės + PVM.

        invoice_number = self.generate_invoice_number()

        invoice = Invoice.objects.create(
            number=invoice_number,
            client=client,
            invoice_type="monthly",
            period_from=period_from,
            period_to=period_to,
            issued_date=issued_date,
            due_date=issued_date + timezone.timedelta(days=14),
            total_amount=Decimal("0.00"),
        )

        vat_rate = Decimal("0.21")
        total_net = Decimal("0.00")

        has_billable_lines = False

        # 1) Abonementai (gali būti keli) — praleidžiam 0.00
        for sub in subscriptions:
            sub_fee = Decimal(str(sub.monthly_fee)).quantize(Decimal("0.01"))
            if sub_fee == Decimal("0.00"):
                continue

            InvoiceLine.objects.create(
                invoice=invoice,
                description=f"{sub.title}",
                quantity=Decimal("1.00"),
                unit_price=sub_fee,
                total=sub_fee,
            )
            total_net += sub_fee
            has_billable_lines = True

        # 2) Papildomi darbai (be PVM)
        for work in work_logs:
            line_total = Decimal(str(work.total_price()))
            InvoiceLine.objects.create(
                invoice=invoice,
                description=work.description,
                quantity=Decimal(str(work.quantity)),
                unit_price=Decimal(str(work.unit_price)),
                total=line_total,
            )
            total_net += line_total
            has_billable_lines = True

            work.billed = True
            work.save(update_fields=["billed"])

        # Jei nėra nei vienos apmokestinamos eilutės (pvz. visi abonementai 0 ir nėra darbų) — sąskaitos nekuriam.
        if not has_billable_lines:
            invoice.delete()
            return

        # 3) PVM (nuo visos sumos be PVM)
        vat_amount = (total_net * vat_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_gross = (total_net + vat_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        invoice.net_amount = total_net
        invoice.vat_rate = vat_rate
        invoice.vat_amount = vat_amount

        # PVM nelaikome kaip atskiros eilutės (InvoiceLine) — jis bus rodomas PDF'e atskirai
        # nuo paslaugų eilučių: Neto suma, PVM, Bruto suma.

        invoice.total_amount = total_gross
        invoice.save(update_fields=["net_amount", "vat_rate", "vat_amount", "total_amount"])

        self.stdout.write(f"Sukurta sąskaita {invoice.number} klientui {client.name}")
        self.stdout.write(
            self.style.SUCCESS(
                f"🗓️ Laikotarpis {period_from}–{period_to}, išrašymo data {issued_date}"
            )
        )
        self.send_invoice_email(invoice)

    def generate_invoice_number(self):
        # Numeracija: MEV26-001, MEV26-002 ...
        prefix = "MEV26"

        last_invoice = (
            Invoice.objects.filter(number__startswith=f"{prefix}-")
            .order_by("-number")
            .first()
        )

        if not last_invoice:
            return f"{prefix}-001"

        last_seq = int(last_invoice.number.split("-")[1])
        return f"{prefix}-{last_seq + 1:03d}"