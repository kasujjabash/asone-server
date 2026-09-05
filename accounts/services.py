"""Business logic for accounts.

Thin views, fat services. Everything here is callable from the API, from an
admin action, from a management command or from a test without going near
HTTP. Nothing in this module imports from accounts.views.
"""

import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from . import permissions as perms
from .models import EmailVerification, LoginAttempt, LoginChallenge

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

    ``password`` is what the lead typed. Omit it and one is generated, which
    is the normal path — the lead reads it once and passes it on, and
    `must_change_password` then forces the owner to replace it at first
    sign-in, because until they do two people know it.

    The generated password is **not emailed**. It travels by whatever route
    the lead uses; the confirmation code goes by email. Two routes, so
    holding both means something.

    The role/site invariant is checked here rather than trusted, because this
    is reachable from the API, the admin and a management command alike.

    Returns ``(user, password)``. The password is never stored in clear text
    and cannot be read back afterwards; showing it to the lead once is the
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


# ---------------------------------------------------------------------------
# Two-factor sign-in
# ---------------------------------------------------------------------------


class NoAccess(Exception):
    """The address is not a user of this system, or has been deactivated.

    Deliberately the *same* exception for both, so the response cannot
    distinguish "never added" from "removed". Central Office deactivates
    staff rather than deleting them, and telling a former employee which of
    the two happened to them is information they have no use for.
    """


class ChallengeUnusable(Exception):
    """Expired, already spent, or out of attempts. Start again."""


def _new_code() -> str:
    """A numeric code, from the OS random source.

    `secrets`, not `random`: the latter is a Mersenne Twister seeded
    predictably enough that watching a handful of codes can reveal the rest.
    Zero-padded, so "004182" is six digits and not four.
    """
    upper = 10 ** settings.LOGIN_CODE_LENGTH
    return str(secrets.randbelow(upper)).zfill(settings.LOGIN_CODE_LENGTH)


def user_with_access(email):
    """The active user for ``email``, or raise NoAccess.

    Called **before** the password is checked, which is what lets the system
    say "you do not have access" rather than "wrong password" to somebody
    who was never added.

    That is a deliberate trade, and worth understanding before changing it:
    it confirms to a caller whether an address is a user here, which is user
    enumeration. It is accepted because this is a closed system of a few
    dozen named accounts created by Central Office — nobody self-registers,
    so the set of users is not a secret worth protecting — and because the
    alternative leaves a teacher who was never added retyping a password
    that was never going to work.

    Two things make it safe enough: `LoginRateThrottle` limits attempts per
    address, and every attempt is recorded in `LoginAttempt`.

    **If AsOne ever opens self-registration, revisit this.** At that point
    the user list stops being a known quantity and the trade stops paying.
    """
    user = User.objects.filter(email__iexact=(email or "").strip()).first()
    if user is None or not user.is_active:
        raise NoAccess(
            "You do not have access to this system. Ask AsOne Central Office "
            "to create an account for you."
        )
    return user


@transaction.atomic
def start_login_challenge(user, *, request=None):
    """Issue a one-time code and email it — the second factor.

    Any earlier unspent challenge for this user is retired first. Without
    that, somebody who asks for three codes in a row could use any of the
    three, which quietly triples the guessing surface and means a code from
    twenty minutes ago still works.

    Returns the challenge. The code itself is returned nowhere and stored
    only as a hash: the email is the only place it exists in readable form.
    """
    LoginChallenge.objects.filter(user=user, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )

    code = _new_code()
    challenge = LoginChallenge.objects.create(
        user=user,
        code_hash=make_password(code),
        expires_at=timezone.now()
        + timedelta(minutes=settings.LOGIN_CODE_TTL_MINUTES),
        ip_address=_client_ip(request) if request else None,
    )

    send_login_code(user, code)
    return challenge


def send_login_code(user, code):
    """Email the code.

    Failures are not swallowed. If the mail cannot be sent the sign-in must
    fail loudly — a caller told "check your email" for a message that was
    never sent has no way to tell that from a slow one, and will sit waiting.
    """
    minutes = settings.LOGIN_CODE_TTL_MINUTES
    send_mail(
        subject=f"Your AsOne sign-in code: {code}",
        message=(
            f"Hello {user.get_full_name() or user.email},\n\n"
            f"Your sign-in code is {code}\n\n"
            f"It expires in {minutes} minutes and can be used once.\n\n"
            "If you did not try to sign in, someone else may know your "
            "password. Tell AsOne Central Office, and change it as soon as "
            "you can.\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def verify_login_code(challenge_id, code):
    """Check a code and spend the challenge. Returns the user.

    Wrong codes count against the attempt limit; a right one consumes the
    challenge so it can never be replayed.

    ## Why the raise happens outside the transaction

    This function was written with `@transaction.atomic` around the whole
    body, which quietly made the attempt limit do nothing: incrementing
    `attempts` and then raising rolled the increment straight back, so every
    guess started from zero and a six-digit code could be worked through at
    leisure. Caught by
    `test_two_factor.py::test_guessing_runs_out_of_tries`.

    So the transaction covers the read and the write, and the refusal is
    raised after it has committed. The lock still does its job — two
    requests cannot both spend one challenge — but a failed attempt is a
    fact that has to survive being refused.

    Raises ChallengeUnusable for anything meaning "start again" — unknown,
    expired, already used, out of attempts. One exception for all of them on
    purpose: the difference is no use to the person typing, and telling an
    attacker which wrong thing they hit is help.
    """
    refusal = None

    with transaction.atomic():
        challenge = (
            LoginChallenge.objects.select_for_update()
            .select_related("user")
            .filter(pk=challenge_id)
            .first()
        )

        if challenge is None or not challenge.is_usable:
            refusal = (
                "That code is no longer valid. Please sign in again to get a new one."
            )
        # Re-checked rather than trusted from the start of the sign-in: an
        # account deactivated in the last ten minutes must not be able to
        # finish a sign-in it had already begun.
        elif not challenge.user.is_active:
            challenge.consumed_at = timezone.now()
            challenge.save(update_fields=["consumed_at"])
            refusal = (
                "That code is no longer valid. Please sign in again to get a new one."
            )
        elif not check_password(code, challenge.code_hash):
            challenge.attempts += 1
            challenge.save(update_fields=["attempts"])
            remaining = settings.LOGIN_CODE_MAX_ATTEMPTS - challenge.attempts
            refusal = (
                "Too many incorrect codes. Please sign in again to get a new one."
                if remaining <= 0
                else f"That code is not correct. {remaining} "
                f"{'try' if remaining == 1 else 'tries'} left."
            )
        else:
            challenge.consumed_at = timezone.now()
            challenge.save(update_fields=["consumed_at"])

    if refusal:
        raise ChallengeUnusable(refusal)

    return challenge.user


# ---------------------------------------------------------------------------
# Email verification — proving a new account's address
# ---------------------------------------------------------------------------


class VerificationUnusable(Exception):
    """Expired, already used, out of attempts, or the code is wrong.

    One exception for all of them, for the same reason `ChallengeUnusable`
    is: the difference is no use to the person typing, and telling somebody
    else which wrong thing they hit is help.
    """


STALE_CODE = (
    "That code is no longer valid. Ask your lead to send the verification "
    "code again."
)


def send_email_verification(user, *, sent_by=None, request=None):
    """Email a code proving this address belongs to this person.

    Any earlier unused code is retired first, so re-sending does not leave
    two working codes.

    The password is deliberately **not** in this email. It reaches the
    person through their lead, by a different route; putting both in one
    inbox would make this step prove nothing.
    """
    EmailVerification.objects.filter(user=user, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )

    code = _new_code()
    verification = EmailVerification.objects.create(
        user=user,
        sent_by=sent_by,
        code_hash=make_password(code),
        expires_at=timezone.now() + timedelta(days=settings.INVITATION_TTL_DAYS),
        ip_address=_client_ip(request) if request else None,
    )

    send_verification_email(user, code, sent_by=sent_by)
    return verification


def send_verification_email(user, code, *, sent_by=None):
    """Tell somebody they have an account and how to confirm the address.

    Failures are not swallowed. If this cannot be sent, creating the account
    must fail loudly — an account whose address was never confirmed cannot
    be signed into, so reporting success would be a lie.
    """
    days = settings.INVITATION_TTL_DAYS
    who = sent_by.get_full_name() if sent_by else "AsOne Central Office"

    send_mail(
        subject="Confirm your AsOne Logistics account",
        message=(
            f"Hello {user.get_full_name() or user.email},\n\n"
            f"{who} has created an account for you on AsOne Logistics, as "
            f"{user.get_role_display()}.\n\n"
            f"Your confirmation code is {code}\n\n"
            "Enter it on the sign-in page to confirm this address. You will "
            "then be able to sign in with the password your lead gave you, "
            "and you will be asked to replace it with one only you know.\n\n"
            "Your password is not in this email, and never will be.\n\n"
            f"The code expires in {days} days. If it runs out, ask your lead "
            "to send another.\n\n"
            "If you were not expecting this, you can ignore it — the account "
            "cannot be used until this code is entered.\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def verify_email(email, code):
    """Confirm the address. Returns the user.

    Marks the account verified and spends the code. After this the person
    can sign in with the password their lead gave them — and will be made to
    replace it, because two people know that one.

    The refusal is raised after the transaction commits, so a failed attempt
    survives being refused — see `verify_login_code` for the bug that taught
    us that.
    """
    refusal = None
    user = None

    with transaction.atomic():
        verification = (
            EmailVerification.objects.select_for_update()
            .select_related("user")
            .filter(user__email__iexact=(email or "").strip())
            .order_by("-created_at")
            .first()
        )

        if verification is None or not verification.is_usable:
            refusal = STALE_CODE
        elif not verification.user.is_active:
            refusal = STALE_CODE
        elif not check_password(code, verification.code_hash):
            verification.attempts += 1
            verification.save(update_fields=["attempts"])
            remaining = settings.LOGIN_CODE_MAX_ATTEMPTS - verification.attempts
            refusal = (
                STALE_CODE
                if remaining <= 0
                else f"That code is not correct. {remaining} "
                f"{'try' if remaining == 1 else 'tries'} left."
            )
        else:
            verification.consumed_at = timezone.now()
            verification.save(update_fields=["consumed_at"])
            user = verification.user
            user.email_verified_at = timezone.now()
            user.save(update_fields=["email_verified_at"])

    if refusal:
        raise VerificationUnusable(refusal)

    return user


class EmailNotVerified(Exception):
    """The account exists and the password is right, but the address was
    never confirmed.

    Raised **after** the password is checked, not before. Checking first
    would tell anybody who typed an address whether it had an unverified
    account, which is more than the closed-door message already gives away.
    """


def require_verified_email(user):
    """Refuse a sign-in for an address nobody has confirmed.

    An account is created with a password the lead knows and an address
    nobody has proven. The code is what proves it, and until it is entered
    the account is not a way in — otherwise a mistyped address would still
    make a working account, reachable by whoever holds the password.
    """
    if not user.email_is_verified:
        raise EmailNotVerified(
            "Your email address has not been confirmed yet. Check your inbox "
            "for the confirmation code and enter it before signing in."
        )
