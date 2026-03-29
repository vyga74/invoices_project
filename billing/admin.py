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
from django.core.management import call_command
from django.urls import path, reverse
from django.shortcuts import redirect

from billing.services.pdf import generate_invoice_pdf

# --- Optimum eksportas ---
import urllib.request
import xml.etree.ElementTree as ET
import ssl
import certifi
import os
from datetime import datetime

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


# --- Eksportas į Optimum (viena eilutė) ---
# Pagal Optimum dokumentaciją, WSDL ir SOAP veikia ir per HTTP.
# Kai kuriuose tinkluose / momentais HTTPS sertifikatas api.optimum.lt gali turėti hostname mismatch,
# todėl leidžiam konfigūruoti URL per ENV ir (jei reikia) naudoti HTTP.

OPTIMUM_TRD_URL = (os.getenv("OPTIMUM_TRD_URL") or "https://api.optimum.lt/v1/lt/Trd.asmx").strip()
OPTIMUM_NS = "http://api.optimum.lt/v1/lt/Trd/"
SOAPENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# Optimum reikalauja sandėlio kodo kiekvienai eilutei (InvArticle.StrFllCode)
OPTIMUM_STR_FLL_CODE = (os.getenv("OPTIMUM_STR_FLL_CODE") or getattr(settings, "OPTIMUM_STR_FLL_CODE", "") or "S").strip()

# Kai kuriose Optimum instaliacijose taip pat reikalaujamas atsakingo darbuotojo kodas (invoice.RspEmpCode)
OPTIMUM_EMP_CODE = (os.getenv("OPTIMUM_EMP_CODE") or getattr(settings, "OPTIMUM_EMP_CODE", "") or "vmil").strip()


def _optimum_ssl_context():
    """SSL context for Optimum SOAP calls.

    Notes:
    - Context is only used for HTTPS.
    - Uses certifi CA bundle to avoid missing/old OS CA stores.
    - You may TEMPORARILY disable verification by setting OPTIMUM_SSL_VERIFY=0 (NOT recommended).
    - If you get hostname mismatch errors, preferred fix is either:
        a) use OPTIMUM_TRD_URL=http://api.optimum.lt/v1/lt/Trd.asmx (no TLS), or
        b) ask Optimum/IT to fix TLS / disable SSL inspection for api.optimum.lt.
    """
    if not OPTIMUM_TRD_URL.lower().startswith("https://"):
        return None

    verify = (getattr(settings, "OPTIMUM_SSL_VERIFY", None) or os.getenv("OPTIMUM_SSL_VERIFY", "1")).strip()
    if verify in {"0", "false", "False", "no", "NO"}:
        return ssl._create_unverified_context()

    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# --- Optimum SOAP helpers ---
def _optimum_request(api_key: str, soap_action: str, body_xml: str) -> bytes:
    """Send a SOAP 1.1 request to Optimum and return raw response bytes."""
    envelope = f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="{SOAPENV_NS}">
  <soap:Header>
    <Header xmlns="{OPTIMUM_NS}">
      <Key>{_xml_escape(api_key)}</Key>
    </Header>
  </soap:Header>
  <soap:Body>
{body_xml}
  </soap:Body>
</soap:Envelope>'''

    data = envelope.encode("utf-8")
    req = urllib.request.Request(
        OPTIMUM_TRD_URL,
        data=data,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": soap_action,
        },
        method="POST",
    )

    ctx = _optimum_ssl_context()
    if ctx is None:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read()



# Debug helper for dumping SOAP request/response for troubleshooting.
def _dump_optimum_soap(prefix: str, request_xml: str, response_bytes: bytes | None) -> None:
    """Best-effort dump of SOAP request/response for debugging (API key should already be masked by caller if needed)."""
    try:
        with open(f"{prefix}_request.xml", "w", encoding="utf-8") as f:
            f.write(request_xml)
    except Exception:
        pass

    if response_bytes is not None:
        try:
            with open(f"{prefix}_response.xml", "wb") as f:
                f.write(response_bytes)
        except Exception:
            pass


def _optimum_insert_cmp_transaction(api_key: str, *, no: str, date_dt: datetime, notes: str = "") -> dict:
    """Create a company transaction in Optimum and return dict with Status/Result/Error.

    Result is expected to be TransactionId (int) on success.
    """
    body_xml = f'''    <InsertCmpTransaction xmlns="{OPTIMUM_NS}">
      <transaction>
        <Date>{date_dt.isoformat()}</Date>
        <No>{_xml_escape(no)}</No>
        <Notes>{_xml_escape(notes)}</Notes>
      </transaction>
    </InsertCmpTransaction>'''

    debug_dump = (os.getenv("OPTIMUM_DEBUG_SOAP", "0").strip() in {"1", "true", "True", "yes", "YES"})
    dump_prefix = f"soap_optimum_trn_{_xml_escape(no).replace('/', '_')}"

    try:
        # Reconstruct full envelope for debugging dumps (API key masked)
        masked_key = "***"
        envelope_for_dump = f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="{SOAPENV_NS}">
  <soap:Header>
    <Header xmlns="{OPTIMUM_NS}">
      <Key>{masked_key}</Key>
    </Header>
  </soap:Header>
  <soap:Body>
{body_xml}
  </soap:Body>
</soap:Envelope>'''

        resp_bytes = _optimum_request(
            api_key=api_key,
            soap_action="http://api.optimum.lt/v1/lt/Trd/InsertCmpTransaction",
            body_xml=body_xml,
        )
    except Exception as exc:
        if debug_dump:
            _dump_optimum_soap(dump_prefix, envelope_for_dump, None)
        return {"Status": "Error", "Result": None, "Error": f"HTTP/SOAP klaida (InsertCmpTransaction): {exc}"}

    try:
        root = ET.fromstring(resp_bytes)
        ns = {"soap": SOAPENV_NS, "opt": OPTIMUM_NS}
        res = root.find(".//opt:InsertCmpTransactionResult", ns)
        if res is None:
            if debug_dump:
                _dump_optimum_soap(dump_prefix, envelope_for_dump, resp_bytes)
            return {"Status": "Error", "Result": None, "Error": "Nepavyko nuskaityti InsertCmpTransactionResult iš SOAP atsakymo."}

        status = (res.findtext("opt:Status", default="", namespaces=ns) or "").strip()
        result = (res.findtext("opt:Result", default="", namespaces=ns) or "").strip() or None
        err = (res.findtext("opt:Error", default="", namespaces=ns) or "").strip() or None

        if status.lower() == "success":
            return {"Status": "Success", "Result": result, "Error": None}
        if debug_dump:
            _dump_optimum_soap(dump_prefix, envelope_for_dump, resp_bytes)
        return {"Status": "Error", "Result": result, "Error": err or "Nežinoma Optimum klaida."}
    except Exception as exc:
        if debug_dump:
            _dump_optimum_soap(dump_prefix, envelope_for_dump, resp_bytes)
        return {"Status": "Error", "Result": None, "Error": f"SOAP parse klaida (InsertCmpTransaction): {exc}"}


def export_invoice_to_optimum_single_line(invoice, *, description: str = "Suteiktos paslaugos (per mėn.)") -> dict:
    """Export a single invoice to Optimum (InsertInvoice) using ONE article line.

    - Uses client identifiers from our DB (company_code/vat_code/name).
    - Sends ONE InvArticle with Qty=1 and UntPrice/ExtPrice = invoice.net_amount.
    - VatTariff is sent as 0.21 (Optimum expects 21% as 0.21).

    Returns dict: {"Status": "Success"|"Error", "Result": <str|None>, "Error": <str|None>}.
    """
    api_key = (getattr(settings, "OPTIMUM_API_KEY", None) or "").strip()
    if not api_key:
        return {"Status": "Error", "Result": None, "Error": "Nėra nustatytas OPTIMUM_API_KEY (settings/ENV)."}

    client = invoice.client

    # Optimum customer identification:
    # - If you later add a dedicated field (e.g. client.optimum_code), swap it here.
    cst_code = (getattr(client, "company_code", None) or "").strip() or (getattr(client, "vat_code", None) or "").strip()
    cst_vat = (getattr(client, "vat_code", None) or "").strip()
    cst_name = (getattr(client, "name", None) or "").strip()

    if not cst_code and not cst_name:
        return {"Status": "Error", "Result": None, "Error": f"Klientui {client!r} trūksta company_code/vat_code ir name."}

    # Dates
    inv_date = getattr(invoice, "issued_date", None) or timezone.localdate()

    # Amounts
    net_amount = (invoice.net_amount or Decimal("0.00")).quantize(Decimal("0.01"))
    if net_amount <= Decimal("0.00"):
        return {"Status": "Error", "Result": None, "Error": f"Sąskaitos {invoice.number} neto suma yra 0.00 – nėra ką eksportuoti."}

    # VAT: Optimum expects 21% as 0.21
    vat_tariff = Decimal("0.21")

    str_fll_code = OPTIMUM_STR_FLL_CODE
    if not str_fll_code:
        return {"Status": "Error", "Result": None, "Error": "Nėra nustatytas OPTIMUM_STR_FLL_CODE (sandėlio kodas)."}

    rsp_emp_code = OPTIMUM_EMP_CODE
    if not rsp_emp_code:
        return {"Status": "Error", "Result": None, "Error": "Nėra nustatytas OPTIMUM_EMP_CODE (RspEmpCode)."}

    cst_grp = (getattr(client, "optimum_cst_group", None) or "K")
    cst_grp = (str(cst_grp)).strip() or "K"

    # Optimum DB pas tave turi FK į dbo.Transactions (fkInvoices01), todėl prieš InsertInvoice
    # susikuriam Transaction ir jo ID perduodam į Invoice.TransactionId.
    trn_no = f"TRN-{invoice.number}"
    trn_resp = _optimum_insert_cmp_transaction(
        api_key,
        no=trn_no,
        date_dt=datetime.now(),
        notes=f"Auto transaction for invoice {invoice.number}",
    )
    if trn_resp.get("Status") != "Success" or not trn_resp.get("Result"):
        return {"Status": "Error", "Result": None, "Error": f"Optimum Transaction nesukurtas: {trn_resp.get('Error')}"}

    try:
        transaction_id = int(str(trn_resp.get("Result")).strip())
    except Exception:
        return {"Status": "Error", "Result": None, "Error": f"Netinkamas TransactionId iš Optimum: {trn_resp.get('Result')}"}

    # Build SOAP body XML (SOAP 1.1) and send
    body_xml = f'''    <InsertInvoice xmlns="{OPTIMUM_NS}">
      <invoice>
        <Date>{inv_date.isoformat()}T00:00:00</Date>
        <No>{_xml_escape(invoice.number)}</No>
        <TransactionId>{transaction_id}</TransactionId>
        <CstCompany>
          <Code>{_xml_escape(cst_code)}</Code>
          <VatCode>{_xml_escape(cst_vat)}</VatCode>
          <Name>{_xml_escape(cst_name)}</Name>
          <CstGrpFllCode>{_xml_escape(cst_grp)}</CstGrpFllCode>
        </CstCompany>
        <RspEmpCode>{_xml_escape(rsp_emp_code)}</RspEmpCode>
        <Notes>{_xml_escape(description)}</Notes>
        <Articles>
          <InvArticle>
            <ArtCode>PRIEZ</ArtCode>
            <StrFllCode>{_xml_escape(str_fll_code)}</StrFllCode>
            <Quantity>1</Quantity>
            <UntPrice>{net_amount}</UntPrice>
            <Discount>0</Discount>
            <VatTariff>{vat_tariff}</VatTariff>
            <ExtPrice>{net_amount}</ExtPrice>
            <Notes>{_xml_escape(description)}</Notes>
          </InvArticle>
        </Articles>
      </invoice>
    </InsertInvoice>'''

    try:
        body = _optimum_request(
            api_key=api_key,
            soap_action="http://api.optimum.lt/v1/lt/Trd/InsertInvoice",
            body_xml=body_xml,
        )
    except Exception as exc:
        return {"Status": "Error", "Result": None, "Error": f"HTTP/SOAP klaida: {exc}"}

    # Parse response
    try:
        root = ET.fromstring(body)
        # Find InsertInvoiceResult node
        ns = {
            "soap": SOAPENV_NS,
            "opt": OPTIMUM_NS,
        }
        res = root.find(".//opt:InsertInvoiceResult", ns)
        if res is None:
            return {"Status": "Error", "Result": None, "Error": "Nepavyko nuskaityti InsertInvoiceResult iš SOAP atsakymo."}

        status = (res.findtext("opt:Status", default="", namespaces=ns) or "").strip()
        result = (res.findtext("opt:Result", default="", namespaces=ns) or "").strip() or None
        err = (res.findtext("opt:Error", default="", namespaces=ns) or "").strip() or None

        # Normalize
        if status.lower() == "success":
            return {"Status": "Success", "Result": result, "Error": None}
        return {"Status": "Error", "Result": result, "Error": err or "Nežinoma Optimum klaida."}
    except Exception as exc:
        return {"Status": "Error", "Result": None, "Error": f"SOAP parse klaida: {exc}"}

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

                    # --- Eksportas į Optimum (viena eilutė) ---
                    optimum_res = export_invoice_to_optimum_single_line(invoice)
                    if optimum_res.get("Status") == "Success":
                        self.message_user(
                            request,
                            f"✅ Optimum: sąskaita {invoice.number} įkelta (Result={optimum_res.get('Result')}).",
                            level=messages.SUCCESS,
                        )
                    else:
                        # Jei dubliuoja numerį – laikom kaip jau įkeltą (nekrentam)
                        err = (optimum_res.get("Error") or "")
                        if "duplicate" in err.lower() or "besidubliuoj" in err.lower():
                            self.message_user(
                                request,
                                f"ℹ️ Optimum: sąskaita {invoice.number} jau egzistuoja (dubliuojasi numeris).",
                                level=messages.INFO,
                            )
                        else:
                            self.message_user(
                                request,
                                f"⚠️ Optimum: nepavyko įkelti {invoice.number}: {optimum_res.get('Error')}",
                                level=messages.WARNING,
                            )

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
        "run_monthly_btn",
    )
    list_filter = ("invoice_type", "paid", "issued_date")
    search_fields = ("number", "client__name")
    inlines = [InvoiceLineInline]
    actions = ["export_selected_to_optimum"]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "run-monthly/",
                self.admin_site.admin_view(self.run_monthly_view),
                name="billing_invoice_run_monthly",
            ),
        ]
        return custom_urls + urls

    def run_monthly_view(self, request):
        """Run monthly invoice generation using the management command."""
        try:
            # Uses the command's default behavior (on the 1st generates for previous month).
            call_command("generate_monthly_invoices")
            # Best-effort: po sugeneravimo pabandom eksportuoti šiandien sukurtas mėnesines sąskaitas.
            today = timezone.localdate()
            latest = Invoice.objects.filter(invoice_type="monthly", issued_date=today).order_by("-id")[:200]
            for inv in latest:
                export_invoice_to_optimum_single_line(inv)
            self.message_user(request, "✅ Mėnesinių sąskaitų generavimas paleistas ir įvykdytas.", level=messages.SUCCESS)
        except Exception as exc:
            self.message_user(request, f"❌ Nepavyko sugeneruoti mėnesinių sąskaitų: {exc}", level=messages.ERROR)
        return redirect("admin:billing_invoice_changelist")

    def run_monthly_btn(self, obj):
        url = reverse("admin:billing_invoice_run_monthly")
        return format_html('<a class="button" href="{}">Generuoti mėnesines</a>', url)

    run_monthly_btn.short_description = "Mėnesinės"

    def pdf_link(self, obj):
        if obj.pdf:
            return format_html('<a href="{}" target="_blank">PDF</a>', obj.pdf.url)
        return "-"

    pdf_link.short_description = "PDF"

    @admin.action(description="Eksportuoti pažymėtas sąskaitas į Optimum (1 eilutė)")
    def export_selected_to_optimum(self, request, queryset):
        ok = 0
        already = 0
        failed = 0

        for inv in queryset.select_related("client"):
            res = export_invoice_to_optimum_single_line(inv)
            if res.get("Status") == "Success":
                ok += 1
            else:
                err = (res.get("Error") or "")
                if "duplicate" in err.lower() or "besidubliuoj" in err.lower():
                    already += 1
                else:
                    failed += 1

        self.message_user(
            request,
            f"Optimum eksportas baigtas: OK={ok}, jau buvo={already}, klaidų={failed}.",
            level=messages.SUCCESS if failed == 0 else messages.WARNING,
        )