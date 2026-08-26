"""Business logic for accounts.

Thin views, fat services. Everything here is callable from the API, from an
admin action, from a management command or from a test without going near
HTTP. Nothing in this module imports from accounts.views.
"""

import secrets
import string

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from . import permissions as perms
from .models import LoginAttempt

User = get_user_model()


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def issue_tokens_for(user) -> dict:
    """Mint a fresh access/refresh pair for ``user``."""
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def blacklist_refresh_token(raw_token: str) -> None:
    """Retire one refresh token, as on logout.

    Raises ``rest_framework_simplejwt.exceptions.TokenError`` if the token is
    malformed, expired or already blacklisted. The caller decides what that
    means over HTTP.

    Note the access token already issued alongside it stays valid until it
    expires — that is inherent to stateless tokens, and the reason
    ACCESS_TOKEN_LIFETIME is 30 minutes rather than a day.
    """
    RefreshToken(raw_token).blacklist()


def revoke_all_refresh_tokens(user) -> int:
    """Blacklist every outstanding refresh token belonging to ``user``.

    Used after a password change: whoever prompted the change — a lost laptop,
    a shared password now being retired — should not keep a working session.
    Returns the number of tokens retired.
    """
    outstanding = OutstandingToken.objects.filter(user=user)
    retired = 0
    for token in outstanding:
        _, created = BlacklistedToken.objects.get_or_create(token=token)
        if created:
            retired += 1
    return retired


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


@transaction.atomic
def change_password(user, new_password: str) -> dict:
    """Set a new password and re-issue tokens.

    The serializer has already checked the current password and run Django's
    validators. This function owns the consequence: old sessions die, and the
    caller gets one fresh pair back so the person changing their password is
    not logged out of the tab they are sitting in.
    """
    user.set_password(new_password)
    # Choosing your own password is exactly what clears the pending state.
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password"])

    revoke_all_refresh_tokens(user)
    return issue_tokens_for(user)


# ---------------------------------------------------------------------------
# What this user may do
# ---------------------------------------------------------------------------

#: The columns of AsOne's access matrix, keyed for the frontend. Built from
#: the same permission classes the API enforces, so the menu the React app
#: draws and the answer the server gives can never drift apart.
ACCESS_MATRIX_COLUMNS = {
    "table_updates": perms.CanUpdateTables,
    "production_orders": perms.CanEnterProductionOrders,
    "warehouse_receiving_and_shipping": perms.CanReceiveAndShip,
    "inventory_adjustments": perms.CanAdjustInventory,
    "school_orders": perms.CanEnterSchoolOrders,
    "backorder_transfers": perms.CanTransferBackorders,
    "financial_reports": perms.CanViewFinancialReports,
}


def access_summary(user) -> dict:
    """Which matrix columns ``user`` holds, and at what scope.

    Purely advisory — it tells the frontend which navigation items to render.
    It is never the thing that protects an endpoint; the permission classes
    and scope_to_user_site() do that, on every request, server-side.
    """
    return {
        "functions": {
            column: perms.has_role(user, *klass.roles)
            for column, klass in ACCESS_MATRIX_COLUMNS.items()
        },
        "scope": _scope_label(user),
    }


def _scope_label(user) -> str:
    """AsOne's own vocabulary for how wide a user's access reaches."""
    return _scope_label_for_role(user.role)


# ---------------------------------------------------------------------------
# Administering other people's accounts
# ---------------------------------------------------------------------------

#: Ambiguous characters are left out. These passwords get read off a screen
#: and typed on a different machine, sometimes written on paper first, and
#: "l" versus "1" versus "I" costs a support call every time.
_PASSWORD_ALPHABET = "".join(
    c for c in string.ascii_letters + string.digits if c not in "lI1O0"
)


def generate_temporary_password(length: int = 12) -> str:
    """A random password for a new or reset account.

    `secrets` rather than `random`: the latter is seeded predictably and is
    not safe for anything a person signs in with.

    The result is shown to the administrator once and never stored in clear
    text. The account it belongs to is flagged `must_change_password`, so it
    stops working the moment its owner picks their own.
    """
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


@transaction.atomic
def create_staff_user(*, password=None, must_change_password=True, **fields):
    """Create a staff account.

    ``password`` is what the lead typed. Omit it and one is generated —
    useful from a management command, and returned so it can be shown once.

    The role/site invariant is checked here rather than trusted, because this
    is reachable from the API, the admin and a management command alike.

    Returns ``(user, password)``. The password is never stored in clear text
    and cannot be read back afterwards; showing it to the lead is the
    caller's job.
    """
    if not password:
        password = generate_temporary_password()

    user = User(**fields, must_change_password=must_change_password)
    user.set_password(password)
    user.full_clean(exclude=["password"])
    user.save()

    return user, password


@transaction.atomic
def set_user_password(user, *, new_password=None, must_change_password=True) -> str:
    """Set another person's password and sign that account out everywhere.

    Covers both "they forgot it" and "a lead is changing it". Pass
    ``new_password`` to use a chosen one, or omit it to have one generated.

    There is no self-service email reset: AsOne's sites are rural, mail
    delivery is not something the system can rely on, and a lead is present
    at every site. A person asks, a lead sets, the person replaces it at
    their next sign-in.

    Returns the password so the caller can show it once.
    """
    if not new_password:
        new_password = generate_temporary_password()

    user.set_password(new_password)
    user.must_change_password = must_change_password
    user.save(update_fields=["password", "must_change_password"])

    # Whoever knew the old password — including whoever prompted the change —
    # must not keep a working session.
    revoke_all_refresh_tokens(user)

    return new_password


@transaction.atomic
def set_active(user, *, is_active: bool) -> None:
    """Activate or deactivate an account.

    Deactivation is how AsOne removes someone's access. Accounts are never
    deleted: the ledger and the audit trail both point at them, and a
    transaction with no user attached would break the promise that every
    movement records who made it.
    """
    user.is_active = is_active
    user.save(update_fields=["is_active"])

    if not is_active:
        revoke_all_refresh_tokens(user)


def force_sign_out(user) -> int:
    """Retire every session belonging to `user`, leaving the account usable.

    For a lost or stolen device, where the person still works here.
    """
    return revoke_all_refresh_tokens(user)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def record_login_attempt(*, email: str, user=None, succeeded: bool, request=None):
    """Write one audit row for a sign-in attempt.

    Recorded for failures as well as successes. A run of failures against one
    account is the only visible sign that someone is guessing at it, and it
    is only visible if the failures are kept.
    """
    return LoginAttempt.objects.create(
        email=(email or "")[:254],
        user=user,
        succeeded=succeeded,
        ip_address=_client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:300] if request else ""),
    )


def _client_ip(request):
    """Best-effort caller address.

    Every AsOne site is behind one shared connection, so this identifies a
    site rather than a person. That is still worth recording — "twenty failed
    sign-ins from Serere overnight" is a useful sentence.

    X-Forwarded-For is only trustworthy once a known proxy sets it. Until
    deployment settles (open question Q12), REMOTE_ADDR is the honest answer.
    """
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


# ---------------------------------------------------------------------------
# The role catalogue
# ---------------------------------------------------------------------------

#: What each role is for, in AsOne's own words. Taken from the "Role Access"
#: sheet of the feature checklist, which in turn reads p.9 of the pack. These
#: strings are shown to a lead choosing a role, so they describe the job
#: rather than the permission bits.
ROLE_SUMMARIES = {
    User.Role.PROGRAM_LEAD: (
        "Everything operational across every warehouse and school. Sets up "
        "master data, places group and production orders, oversees receiving "
        "and shipping, reads every report."
    ),
    User.Role.OPERATIONS_MANAGER: (
        "The same access as a Program Lead. Day to day this is the role "
        "running production orders and warehouse operations."
    ),
    User.Role.WAREHOUSE_STAFF: (
        "One warehouse. Enters receipts from the Tailoring Centers, picks and "
        "packs school orders, prints pick and packing lists, and sees their "
        "own stock position."
    ),
    User.Role.SCHOOL_STAFF: (
        "One school. Places student orders at kit or item level, captures the "
        "student name, generates invoices, and tracks what has shipped."
    ),
    User.Role.FINANCE: (
        "Posts every kind of inventory adjustment with a reason code, and "
        "reads the costed reports across all locations."
    ),
}

#: Written where the answer is a deliberate reading of AsOne's matrix rather
#: than something they stated outright, so the UI can show it and a lead is
#: not surprised later.
ROLE_CAVEATS = {
    User.Role.PROGRAM_LEAD: "Cannot post inventory adjustments — the matrix reserves those for Finance.",
    User.Role.OPERATIONS_MANAGER: "Cannot post inventory adjustments — the matrix reserves those for Finance.",
    User.Role.WAREHOUSE_STAFF: "Cannot see the other warehouse's stock, change master data, or enter school orders.",
    User.Role.SCHOOL_STAFF: "Cannot see inventory, other schools, or any cost beyond their own price list.",
    User.Role.FINANCE: "Cannot enter production orders, receipts, school orders or shipments, and cannot change master data.",
}


def role_catalogue() -> list:
    """Every role, what it may do, and which site it needs.

    Published so the React app can build its role picker, decide whether to
    show a warehouse or a school selector, and render navigation — without
    restating the access matrix in TypeScript, where it would drift.

    Derived from the same permission classes the API enforces, so it cannot
    disagree with them.
    """
    return [
        {
            "value": role.value,
            "label": role.label,
            "summary": ROLE_SUMMARIES.get(role, ""),
            "caveat": ROLE_CAVEATS.get(role, ""),
            "scope": _scope_label_for_role(role),
            # "warehouse", "school", or null. Drives which picker the
            # user-creation form shows, and which one it must not show.
            "requires_site": User.required_site_field(role),
            "functions": {
                column: role in klass.roles
                for column, klass in ACCESS_MATRIX_COLUMNS.items()
            },
        }
        for role in User.Role
    ]


def _scope_label_for_role(role) -> str:
    """AsOne's vocabulary for how wide a role reaches."""
    if role in User.ALL_SITE_ROLES:
        return "all_locations"
    if role == User.Role.WAREHOUSE_STAFF:
        return "assigned_warehouse"
    if role == User.Role.SCHOOL_STAFF:
        return "assigned_schools"
    return "none"
