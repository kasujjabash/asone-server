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


class CanConfirmPayment(BasePermission):
    """Who may release an order off Hold — F35, and the seam for question Q2.

    **This class is the whole of the open question.** AsOne's chart says an
    order waits until "School Monitor" confirms the invoice is paid, and
    nobody has told us what School Monitor is. `release_order()` does not
    care: it records who confirmed payment and when, whoever that turns out
    to be. Only this class has to change.

    Coded as **Finance**, on the reasoning that confirming money has arrived
    is a finance act and Finance already holds every other money-adjacent
    column on the matrix. That is our reading, not AsOne's instruction.

    When Q2 is answered:

        a person at Central Office  -> add their role to `roles`
        the school itself           -> add Role.SCHOOL_STAFF, and note that
                                       the payer would then be confirming
                                       their own payment — worth raising
                                       before doing it
        another system              -> replace this class with token
                                       authentication on the release view

    Deliberately *not* School Staff today: a school marking its own invoice
    paid is a control decision AsOne has not made.
    """

    message = "Your role cannot confirm payment on a school order."

    roles = frozenset({Role.FINANCE})

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, *self.roles)


class CanReadBackorderReport(BasePermission):
    """F49 — outstanding backorders. The widest of the fulfilment reports.

    The checklist gives it to everybody, and it is the one report where that
    is obviously right: a school is waiting for it, a warehouse is chasing
    it, and Finance is carrying it as an unfilled invoice.

        leads     all sites        warehouse  own warehouse
        school    own schools      Finance    view only

    The row-level half is `scope_to_user_site()`; this is the door.
    """

    message = "Your role does not have access to the backorder report."

    def has_permission(self, request, view) -> bool:
        return has_role(
            request.user,
            *ALL_SITE_ROLES,
            Role.FINANCE,
            Role.WAREHOUSE_STAFF,
            Role.SCHOOL_STAFF,
        )


class CanReadFulfilmentReports(BasePermission):
    """F52 and F54 — orders picked but not despatched.

    The checklist lists them as two features with two audiences:

        F52  leads, warehouse (own)                — no school, no Finance
        F54  leads, warehouse (own), school (own)  — no Finance

    They ask the same question of the data, so they are served by one
    endpoint, and this is the union — which is exactly F54. F52's blank
    School Staff cell is moot: F54 grants a school the same rows anyway, so
    honouring F52's narrower cell would deny data the next row along grants.

    **Finance is excluded from both**, which is deliberate on AsOne's part
    and worth knowing before somebody "fixes" it: they get the costed
    reports, not the operational backlog.
    """

    message = "Your role does not have access to the fulfilment reports."

    def has_permission(self, request, view) -> bool:
        return has_role(
            request.user, *ALL_SITE_ROLES, Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF
        )


class CanReadPackingList(BasePermission):
    """F40 — the document that travels with the goods.

    The checklist gives it to the leads and the warehouse that packs it, and
    leaves the School Staff cell blank.

    **That is worth querying with AsOne.** Their definitions page says the
    school uses the invoice number and student name to hand shipments to the
    right child, which is what a packing list is for — so a school arguably
    needs to see it. The likeliest reading is that the school gets the
    *printed* sheet in the box rather than a screen, and that is how it is
    coded: as written, leads and warehouse only.

    If they say the school should see it, add Role.SCHOOL_STAFF here and
    nothing else changes.
    """

    message = "Your role does not have access to packing lists."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, *ALL_SITE_ROLES, Role.WAREHOUSE_STAFF)


class CanReadSchoolOrders(BasePermission):
    """Read-only views over orders — the F53 report and anything like it.

    Leads and Finance see every school; a school clerk sees their own. The
    row-level half is `scope_to_user_site()`; this only decides who gets
    through the door.
    """

    message = "Your role does not have access to school order reports."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, *ALL_SITE_ROLES, Role.FINANCE, Role.SCHOOL_STAFF)
