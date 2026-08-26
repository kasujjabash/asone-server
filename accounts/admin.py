"""Central Office screens for staff accounts and the sign-in audit trail."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import LoginAttempt, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Staff accounts.

    The field lists are declared in full rather than extended from
    BaseUserAdmin's, because those refer to `username`, which this project
    does not have — staff sign in with their email address.
    """

    ordering = ("email",)
    search_fields = ("email", "first_name", "last_name")
    list_display = ("email", "first_name", "last_name", "role", "site", "is_active")
    list_filter = ("role", "is_active", "warehouse", "school")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal details", {"fields": ("first_name", "last_name")}),
        (
            "AsOne role and site",
            {
                "fields": ("role", "warehouse", "school", "must_change_password"),
                "description": (
                    "Warehouse staff need a warehouse and school staff need a "
                    "school. The all-locations roles take neither."
                ),
            },
        ),
        (
            "Permissions",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
                "classes": ("collapse",),
                "description": (
                    "Django-level flags. Day-to-day access is governed by the "
                    "role above, not by these."
                ),
            },
        ),
        ("Dates", {"fields": ("last_login", "date_joined"), "classes": ("collapse",)}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "first_name", "last_name", "role", "warehouse", "school"),
            },
        ),
        (None, {"classes": ("wide",), "fields": ("password1", "password2")}),
    )

    @admin.display(description="Site")
    def site(self, obj):
        return obj.warehouse or obj.school or "All locations"

    @admin.action(description="Sign selected users out of every session")
    def sign_out(self, request, queryset):
        from . import services

        retired = sum(services.force_sign_out(user) for user in queryset)
        self.message_user(request, f"{retired} session(s) retired.")

    @admin.action(description="Deactivate selected users")
    def deactivate(self, request, queryset):
        from . import services

        for user in queryset.exclude(pk=request.user.pk):
            services.set_active(user, is_active=False)
        self.message_user(request, "Deactivated, and signed out of every session.")

    actions = ["sign_out", "deactivate"]


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    """Read-only. The audit trail is append-only and this screen must not
    become a way to edit it."""

    list_display = ("at", "email", "succeeded", "ip_address")
    list_filter = ("succeeded", "at")
    search_fields = ("email", "ip_address")
    date_hierarchy = "at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
