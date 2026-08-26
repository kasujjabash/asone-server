"""Role and site access control.

Single source of truth: the access matrix on p.9 of the 14 August pack
("As One Logistics -TC Uniforms 2026-08-14.pdf"), cross-checked against the
"Role Access" sheet of AsOne_Logistics_System_Checklist.xlsx.

That matrix is a grid of roles down the side and *functions* across the top.
The capability classes below are named after those columns, one class per
column, so when AsOne changes a cell the change here is one line:

                        | Table    | Production | Warehouse  | Inventory | School   | Backorder | Financial
                        | Updates  | Orders     | Recv/Ship  | Adj       | Orders   | Transfers | Reports
    --------------------+----------+------------+------------+-----------+----------+-----------+-----------
    Program Lead        | All      | All        | All        |     -     |    -     | All       | All
    Operations Manager  | All      | All        | All        |     -     |    -     | All       | All
    Warehouse Staff     |   -      |     -      | Assigned   |     -     |    -     | Assigned* |  -
    School Staff        |   -      |     -      |     -      |     -     | Assigned |    -      |  -
    Finance Department  |   -      |     -      |     -      | All       |    -     |    -      | All

    All      = every site        Assigned = own warehouse / own schools
    -        = no access

    * The Warehouse Staff / Backorder Transfers cell is blank in AsOne's
      printed matrix. It is granted here on Jim's 24 August direction, which
      supersedes it. See docs/CLIENT_DECISIONS.md D5.

Two separate questions are answered in this module, and they must not be
confused with each other:

    *What* may this user do?   -> the permission classes
    *Which rows* may they see? -> scope_to_user_site()

A permission class lets a warehouse clerk open the receiving screen. Only
scope_to_user_site() stops that clerk seeing the other warehouse's receipts.
Every list endpoint needs both.
"""

from rest_framework.permissions import BasePermission, IsAuthenticated

from .models import User

Role = User.Role

#: Roles the matrix scopes to "All Locations" for the operational columns.
#: Two roles rather than one because AsOne reports on them separately, not
#: because their access differs — every operational cell is identical.
ALL_SITE_ROLES = frozenset({Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER})


def has_role(user, *roles) -> bool:
    """True when an authenticated, active user holds one of ``roles``."""
    return bool(user and user.is_authenticated and user.is_active and user.role in roles)


def sees_all_sites(user) -> bool:
    """True for roles whose scope is "All Locations" in every column they hold.

    Finance is included: its two columns (Inventory Adj, Financial Reports)
    are both All Locations.
    """
    return has_role(user, *ALL_SITE_ROLES, Role.FINANCE)


# ---------------------------------------------------------------------------
# Role identity
# ---------------------------------------------------------------------------
# Use these only when a rule genuinely turns on *who someone is*. Anything
# that turns on *what they may do* belongs in a capability class below, so
# that a matrix change lands in one place.


class _RolePermission(BasePermission):
    """Base for the checks in this module. Not used directly on views."""

    roles: frozenset = frozenset()
    message = "Your role does not have access to this function."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, *self.roles)


class IsProgramLead(_RolePermission):
    roles = frozenset({Role.PROGRAM_LEAD})


class IsOperationsManager(_RolePermission):
    roles = frozenset({Role.OPERATIONS_MANAGER})


class IsWarehouseStaff(_RolePermission):
    roles = frozenset({Role.WAREHOUSE_STAFF})


class IsSchoolStaff(_RolePermission):
    roles = frozenset({Role.SCHOOL_STAFF})


class IsFinance(_RolePermission):
    roles = frozenset({Role.FINANCE})


# ---------------------------------------------------------------------------
# One class per column of the access matrix
# ---------------------------------------------------------------------------


class CanUpdateTables(_RolePermission):
    """Matrix column: "Table Updates".

    The master data Central Office owns — garments, sizes, SKUs, prices,
    uniform kits, sites, reason codes, users.

    Role Access sheet: Warehouse Staff "cannot change master data or prices";
    Finance "cannot change master data".
    """

    roles = ALL_SITE_ROLES


class CanEnterProductionOrders(_RolePermission):
    """Matrix column: "Production Orders Entry".

    Covers the group order too — p.4 makes the production orders a breakdown
    of it, and the matrix gives them a single column.

    Role Access sheet: Finance "cannot enter production orders".
    """

    roles = ALL_SITE_ROLES


class CanReceiveAndShip(_RolePermission):
    """Matrix column: "Warehouse Receiving and Shipping".

    Receipts against a production order, pick lists, packing lists, shipments.
    Warehouse Staff hold this for their assigned warehouse only — the class
    grants the function, scope_to_user_site() confines it to their site.

    Tailoring Centers are not system users, so a warehouse always keys in the
    receipt from the TC's handwritten packing list.
    """

    roles = ALL_SITE_ROLES | {Role.WAREHOUSE_STAFF}


class CanAdjustInventory(_RolePermission):
    """Matrix column: "Inventory Adj".

    Corrections, warehouse transfers, returns and damages, each with a reason
    code.

    OPEN QUESTION Q3 — AsOne gives this column to Finance alone, so neither
    lead can correct a miscount and the warehouse that does the counting
    cannot post the result. Coded as they wrote it. If they widen the cell,
    widen this set.
    """

    roles = frozenset({Role.FINANCE})


class CanEnterSchoolOrders(_RolePermission):
    """Matrix column: "School Orders Entry".

    The point of sale. School Staff only — the matrix leaves this cell blank
    for both leads, and the Role Access sheet omits POS from their screens.

    OPEN QUESTION Q7 — whether schools have the computers and connectivity to
    place their own orders at all. If they do not, someone else keys these in
    and this cell changes.
    """

    roles = frozenset({Role.SCHOOL_STAFF})


class CanTransferBackorders(_RolePermission):
    """Matrix column: "Backorder Transfers".

    Warehouse Staff hold this despite the cell being blank in AsOne's printed
    matrix (p.9). Jim, 24 August 2026:

        "The warehouses should have the capability to transfer 'Backorders'
        to another warehouse with Inventory. The fulfilling warehouse will
        then ship directly to the appropriate school."

    That is a direct statement from AsOne and it supersedes the printed cell.
    Recorded as D5 in docs/CLIENT_DECISIONS.md.

    Note this is the one capability a warehouse user holds that reaches past
    their own site, so the transfer endpoint will need a deliberately narrow
    cross-site read — see D5 for the constraint.
    """

    roles = ALL_SITE_ROLES | {Role.WAREHOUSE_STAFF}


class CanViewFinancialReports(_RolePermission):
    """Matrix column: "Financial Reports".

    The costed reports — group orders, receipts, shipments, adjustments.

    Role Access sheet: School Staff "cannot see costs beyond their own price
    list", so their price list is not served through this permission.
    """

    roles = ALL_SITE_ROLES | {Role.FINANCE}


class PasswordChangeNotPending(BasePermission):
    """Block a user who is still on an administrator-issued password.

    When a lead creates an account or resets one, the new password is known to
    both of them. Until the owner replaces it, that is a shared password —
    the thing AsOne explicitly ruled out (p.9). So the account can do exactly
    three things: read itself, set a new password, and sign out.

    Views that must stay reachable while a change is pending set
    ``allow_password_change_pending = True`` on the class.
    """

    message = "Set a new password before using the system."

    def has_permission(self, request, view) -> bool:
        if getattr(view, "allow_password_change_pending", False):
            return True

        user = request.user
        if not (user and user.is_authenticated):
            # Not our decision to make — IsAuthenticated answers this one.
            return True

        return not user.must_change_password


#: The standard pair for any authenticated endpoint. Use this rather than a
#: bare ``IsAuthenticated``, so a new view cannot forget the pending-password
#: check and quietly become reachable on a shared password.
AUTHENTICATED = [IsAuthenticated, PasswordChangeNotPending]


# ---------------------------------------------------------------------------
# Row-level scoping
# ---------------------------------------------------------------------------


def scope_to_user_site(queryset, user, *, warehouse_field=None, school_field=None):
    """Narrow ``queryset`` to the sites ``user`` is allowed to see.

    Call this from ``get_queryset()`` on every endpoint that returns site
    data. Pass the ORM path from the model being listed to the site it
    belongs to::

        # Receipt has a direct FK to Warehouse
        scope_to_user_site(qs, user, warehouse_field="warehouse")

        # Shipment reaches a school through its order
        scope_to_user_site(qs, user, school_field="order__school")

    Deny-by-default is deliberate: anything unrecognised returns ``none()``.
    A role added to ``User.Role`` without a branch here shows an empty list,
    which someone reports as a bug. The opposite default would quietly show
    them every site instead, and nobody reports that.
    """
    if not (user and user.is_authenticated and user.is_active):
        return queryset.none()

    # Program Lead, Operations Manager, Finance — "All Locations".
    if sees_all_sites(user):
        return queryset

    if user.role == Role.WAREHOUSE_STAFF:
        # A warehouse user with no warehouse set is a misconfiguration, not a
        # licence to see every site.
        if warehouse_field is None or user.warehouse_id is None:
            return queryset.none()
        return queryset.filter(**{warehouse_field: user.warehouse_id})

    if user.role == Role.SCHOOL_STAFF:
        if school_field is None or user.school_id is None:
            return queryset.none()
        return queryset.filter(**{school_field: user.school_id})

    return queryset.none()
