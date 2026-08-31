"""Central Office view of school orders.

Read-mostly. An order is placed at the school through the point of sale;
this exists so Central Office can look one up when a parent rings about an
invoice number.
"""

from django.contrib import admin

from .models import SchoolOrder, SchoolOrderLine


class SchoolOrderLineInline(admin.TabularInline):
    model = SchoolOrderLine
    extra = 0
    fields = ("sku", "quantity", "unit_price", "from_kit")
    readonly_fields = fields
    can_delete = False


@admin.register(SchoolOrder)
class SchoolOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "student_name", "school", "order_date", "status", "total")
    list_filter = ("status", "school", "order_date")
    search_fields = ("number", "student_name")
    date_hierarchy = "order_date"
    readonly_fields = ("number", "total", "created_by", "created_at")
    inlines = [SchoolOrderLineInline]

    @admin.display(description="Total")
    def total(self, obj):
        return obj.total

    def has_add_permission(self, request):
        """Orders are placed at the school, through the point of sale — an
        order typed here would have no student behind it."""
        return False

    def has_delete_permission(self, request, obj=None):
        """The number is an invoice a parent is holding."""
        return False
