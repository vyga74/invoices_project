from django.contrib import admin
from .models import Client, Subscription, ClientEmail, WorkLog
from django.utils.html import format_html

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

    def total(self, obj):
        return obj.total_price()
    total.short_description = "Suma (€)"

from .models import Invoice, InvoiceLine


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