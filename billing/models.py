from django.db import models


class Client(models.Model):
    name = models.CharField("Pavadinimas", max_length=255)
    company_code = models.CharField("Įmonės kodas", max_length=50, blank=True)
    vat_code = models.CharField("PVM kodas", max_length=50, blank=True)
    email = models.EmailField("El. paštas", blank=True)
    address = models.TextField("Adresas", blank=True)
    active = models.BooleanField("Aktyvus", default=True)

    def __str__(self):
        return self.name


class Subscription(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        verbose_name="Klientas",
    )

    title = models.CharField("Už ką (pavadinimas)", max_length=255)

    monthly_fee = models.DecimalField(
        "Mėnesinis abonementas (€)",
        max_digits=10,
        decimal_places=2,
    )

    hosting_yearly_fee = models.DecimalField(
        "Hostingo kaina metams (€)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    hosting_valid_until = models.DateField(
        "Hostingo apmokėjimas galioja iki",
        null=True,
        blank=True,
    )

    active = models.BooleanField("Aktyvus", default=True)

    def __str__(self):
        return f"{self.client.name}: {self.title}"


class ClientEmail(models.Model):
    EMAIL_TYPE_CHOICES = [
        ("accounting", "Buhalterija"),
        ("administration", "Administracija"),
        ("other", "Kita"),
    ]

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="emails",
        verbose_name="Klientas",
    )

    email = models.EmailField("El. paštas")
    email_type = models.CharField(
        "Tipas",
        max_length=50,
        choices=EMAIL_TYPE_CHOICES,
        default="other",
    )
    active = models.BooleanField("Aktyvus", default=True)

    def __str__(self):
        return f"{self.client.name} – {self.email} ({self.get_email_type_display()})"


class WorkLog(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="work_logs",
        verbose_name="Klientas",
    )

    date = models.DateField("Data")

    description = models.CharField(
        "Darbo aprašymas",
        max_length=255,
    )

    quantity = models.DecimalField(
        "Kiekis / valandos",
        max_digits=8,
        decimal_places=2,
        default=1,
    )

    unit_price = models.DecimalField(
        "Kaina už vienetą (€)",
        max_digits=10,
        decimal_places=2,
    )

    billed = models.BooleanField(
        "Įtraukta į sąskaitą",
        default=False,
    )


    def total_price(self):
        return self.quantity * self.unit_price

    def __str__(self):
        return f"{self.client.name} – {self.date} – {self.description}"


class Invoice(models.Model):
    INVOICE_TYPE_CHOICES = [
        ("monthly", "Mėnesinė"),
        ("hosting", "Hostingo avansinė"),
    ]

    number = models.CharField("Numeris", max_length=50, unique=True)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="invoices")
    invoice_type = models.CharField("Tipas", max_length=20, choices=INVOICE_TYPE_CHOICES)
    period_from = models.DateField("Periodas nuo")
    period_to = models.DateField("Periodas iki")
    issued_date = models.DateField("Išrašymo data")
    due_date = models.DateField("Apmokėti iki")

    net_amount = models.DecimalField(
        "Suma be PVM (€)",
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    vat_rate = models.DecimalField(
        "PVM tarifas",
        max_digits=5,
        decimal_places=4,
        default=0.21,
    )

    vat_amount = models.DecimalField(
        "PVM suma (€)",
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    total_amount = models.DecimalField("Suma su PVM (€)", max_digits=12, decimal_places=2)
    paid = models.BooleanField("Apmokėta", default=False)
    pdf = models.FileField("PDF", upload_to="invoices/%Y/%m/", blank=True, null=True)

    def __str__(self):
        return f"{self.number} – {self.client.name}"


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField("Aprašymas", max_length=255)
    quantity = models.DecimalField("Kiekis", max_digits=8, decimal_places=2)
    unit_price = models.DecimalField("Kaina (€)", max_digits=10, decimal_places=2)
    total = models.DecimalField("Suma (€)", max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.invoice.number} – {self.description}"
