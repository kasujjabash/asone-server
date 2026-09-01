"""Who may see and place school orders.

Kept here rather than in `accounts/permissions.py` for two reasons. The
practical one: Denis is working in that file's neighbourhood, and this avoids
us both editing it. The better one: these are not matrix *columns*, they are
the access rows of three individual features that happen to disagree with
each other —

    F30-F33  placing an order        School Staff only
    F34      the invoice             School Staff, and Finance read-only
    F53      the on-hold report      the leads too

The columns live in `accounts/permissions.py`. This is the finer grain.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from accounts.models import User
from accounts.permissions import ALL_SITE_ROLES, has_role

Role = User.Role


class SchoolOrderAccess(BasePermission):
    """Placing an order is School Staff only. Reading it is wider.

    AsOne leaves the School Orders Entry column blank for both leads, and
    the Role Access sheet omits the point of sale from their screens — so
    nobody but a school clerk creates or cancels an order.

    Reading is granted per view, because the features disagree: F34 gives
    Finance a view of the invoice, F53 gives the leads a view of what is
    still on hold. A view that declares no `read_roles` is readable by
    school staff alone, which is the safe default — forgetting the
    attribute narrows access rather than opening it.
    """

    message = "Only school staff can place or change a school order."

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            audience = frozenset(getattr(view, "read_roles", ())) | {Role.SCHOOL_STAFF}
            return has_role(request.user, *audience)

        return has_role(request.user, Role.SCHOOL_STAFF)


class CanReadSchoolOrders(BasePermission):
    """Read-only views over orders — the F53 report and anything like it.

    Leads and Finance see every school; a school clerk sees their own. The
    row-level half is `scope_to_user_site()`; this only decides who gets
    through the door.
    """

    message = "Your role does not have access to school order reports."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, *ALL_SITE_ROLES, Role.FINANCE, Role.SCHOOL_STAFF)
