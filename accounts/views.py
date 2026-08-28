"""HTTP layer for authentication.

Views translate between HTTP and the rest of the system and do nothing else:
validate with a serializer, call a service, choose a status code. Any logic
worth testing lives in accounts/services.py, where a test can reach it
without building a request.
"""

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from . import services
from .models import LoginAttempt, User
from .permissions import AUTHENTICATED, CanUpdateTables
from .throttling import LoginBurstRateThrottle, LoginRateThrottle
from .serializers import (
    LoginAttemptSerializer,
    LoginSerializer,
    LogoutSerializer,
    MeUpdateSerializer,
    PasswordChangeSerializer,
    RoleSerializer,
    SetPasswordSerializer,
    UserAdminSerializer,
    UserCreateSerializer,
    UserSerializer,
)


# ---------------------------------------------------------------------------
# Getting a token
# ---------------------------------------------------------------------------


@extend_schema(
    tags=["Authentication"],
    summary="Sign in",
    description=(
        "Exchange an email address and password for an access token, a "
        "refresh token and the signed-in user's record, including role, site "
        "and access summary."
    ),
)
class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    permission_classes = [AllowAny]
    # Two limits, both of which must pass: per email address, and a looser one per
    # IP address. See accounts/throttling.py for why a per-IP limit alone
    # would lock out a whole warehouse.
    throttle_classes = [LoginRateThrottle, LoginBurstRateThrottle]

    def post(self, request, *args, **kwargs):
        email = request.data.get("email") if hasattr(request, "data") else None

        try:
            response = super().post(request, *args, **kwargs)
        except Exception:
            # Recorded before re-raising, so a failure is audited even though
            # the caller only ever sees a 401.
            services.record_login_attempt(
                email=email,
                user=User.objects.filter(email__iexact=email or "").first(),
                succeeded=False,
                request=request,
            )
            raise

        services.record_login_attempt(
            email=email,
            user=User.objects.filter(email__iexact=email or "").first(),
            succeeded=True,
            request=request,
        )
        return response


@extend_schema(
    tags=["Authentication"],
    summary="Refresh an access token",
    description=(
        "Exchange a valid refresh token for a new access token. Refresh "
        "tokens rotate: the one sent is blacklisted and a replacement is "
        "returned alongside the new access token."
    ),
)
class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    # Its own budget. Sharing the login scope would mean a site's routine
    # token refreshes ate the allowance its staff need to sign in.
    throttle_scope = "token_refresh"


@extend_schema(
    tags=["Authentication"],
    summary="Verify a token",
    description="Check whether a token is still valid. Returns 200 or 401.",
)
class VerifyView(TokenVerifyView):
    permission_classes = [AllowAny]


# ---------------------------------------------------------------------------
# Giving it back
# ---------------------------------------------------------------------------


@extend_schema(
    tags=["Authentication"],
    summary="Sign out",
    request=LogoutSerializer,
    responses={
        205: OpenApiResponse(description="Refresh token blacklisted."),
        400: OpenApiResponse(description="The token was missing, malformed or already used."),
    },
    description=(
        "Blacklist the refresh token so it cannot be exchanged again. The "
        "access token issued with it stays valid until it expires, which is "
        "why access tokens are short-lived."
    ),
)
class LogoutView(APIView):
    permission_classes = [IsAuthenticated]
    # Signing out must never be blocked. An account that cannot sign out is
    # an account whose token stays live until it expires.
    allow_password_change_pending = True

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.blacklist_refresh_token(serializer.validated_data["refresh"])
        except TokenError:
            # Deliberately not specific about why. A caller probing tokens
            # learns only that this one is unusable.
            return Response(
                {"detail": "That refresh token is not valid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(status=status.HTTP_205_RESET_CONTENT)


# ---------------------------------------------------------------------------
# The signed-in user
# ---------------------------------------------------------------------------


@extend_schema(tags=["Authentication"])
class MeView(RetrieveUpdateAPIView):
    """Read or edit the signed-in user's own record.

    There is no lookup by id and no queryset to filter — the object is always
    ``request.user``, so one user can never address another through this
    endpoint. Writes go through MeUpdateSerializer, whose field list excludes
    role and site.
    """

    permission_classes = [IsAuthenticated]
    # Reachable on a pending password so the React app can read the flag and
    # route to the change-password screen rather than showing a wall of 403s.
    allow_password_change_pending = True
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        return UserSerializer if self.request.method == "GET" else MeUpdateSerializer

    @extend_schema(
        summary="Current user",
        responses=UserSerializer,
        description="The signed-in user with their role, site and access summary.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="Update your own details",
        request=MeUpdateSerializer,
        responses=UserSerializer,
        description=(
            "Change your name or email address. Role and site are set by an "
            "administrator and cannot be changed here."
        ),
    )
    def patch(self, request, *args, **kwargs):
        super().patch(request, *args, **kwargs)
        # Answer with the full record rather than the three fields that were
        # writable, so the client can refresh its copy of the user in one go.
        return Response(UserSerializer(request.user).data)


@extend_schema(
    tags=["Authentication"],
    summary="Change your password",
    request=PasswordChangeSerializer,
    responses={
        200: OpenApiResponse(description="Password changed. A new token pair is returned."),
        400: OpenApiResponse(description="Current password wrong, or new password rejected."),
    },
    description=(
        "Requires the current password even though the request is already "
        "authenticated. On success every existing session is signed out and a "
        "fresh token pair is returned for the session making the change."
    ),
)
class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    # The whole point of the pending state is to get here.
    allow_password_change_pending = True
    throttle_scope = "password_change"

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        tokens = services.change_password(
            request.user, serializer.validated_data["new_password"]
        )
        return Response({"detail": "Password changed.", **tokens}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Administering other people's accounts
# ---------------------------------------------------------------------------


@extend_schema(tags=["User administration"])
class UserViewSet(viewsets.ModelViewSet):
    """Create and manage staff accounts.

    Behind ``CanUpdateTables`` — the matrix column that owns the User table
    (p.3), held by Program Lead and Operations Manager. No site scoping is
    applied because those are the only two roles that reach this at all, and
    both are "All Locations". If AsOne ever widens the column, add
    ``scope_to_user_site()`` to ``get_queryset()`` before doing anything else.

    Accounts are never deleted. DELETE is not routed: the stock ledger and the
    audit trail both point at users, and a movement with no user attached
    would break the promise that every transaction records who made it. Use
    ``deactivate`` instead.
    """

    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    queryset = User.objects.select_related("warehouse", "school").order_by("email")
    filterset_fields = ["role", "is_active", "warehouse", "school"]

    # No DELETE. Accounts are deactivated, never removed: the sign-in audit
    # trail points at them, and every stock movement will once the ledger
    # exists. Only LoginAttempt protects a user today, so without this an
    # account that had never signed in could be erased.
    http_method_names = ["get", "post", "patch", "put", "head", "options"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action == "set_password":
            return SetPasswordSerializer
        return UserAdminSerializer

    def _guard_self(self, target, what: str):
        """Refuse an administrator changing this aspect of their own account.

        Not paranoia about privilege escalation — a lead already holds the
        highest access. It is about lockout: a lead who deactivates or demotes
        themselves needs another lead to undo it, and there may not be one
        signed in at that site.
        """
        if target.pk == self.request.user.pk:
            raise PermissionDenied(f"You cannot {what} your own account.")

    @extend_schema(
        summary="Add a user",
        request=UserCreateSerializer,
        responses={201: OpenApiResponse(description="Created.")},
        description=(
            "First name, last name, email address, role and password. The "
            "email address is the credential the person signs in with, so it "
            "must be unique.\n\n"
            "Warehouse staff also need a `warehouse`, and school staff a "
            "`school`. The all-locations roles take neither — see "
            "`GET /api/auth/roles/` for which is which.\n\n"
            "By default the account must replace this password at first "
            "sign-in, because until then you know it too."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user, password = services.create_staff_user(**serializer.validated_data)

        return Response(
            {
                "user": UserAdminSerializer(user).data,
                "password": password,
                "detail": (
                    "Give these credentials to the user. The password is not "
                    "stored in readable form and cannot be shown again."
                ),
            },
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        # Checked before the serializer runs. A lead patching their own role
        # should be told plainly that they cannot, not handed a validation
        # error about some other field in the same payload.
        target = self.get_object()
        if "role" in request.data:
            self._guard_self(target, "change the role of")
        if request.data.get("is_active") is False:
            self._guard_self(target, "deactivate")

        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Set a user's password",
        request=SetPasswordSerializer,
        responses={200: OpenApiResponse(description="Password set.")},
        description=(
            "Changes another person's password — for someone who has "
            "forgotten theirs, or when an account needs a new one. The "
            "current password is not required: the point is that nobody has "
            "it.\n\n"
            "Signs that account out of every session. Send `new_password` to "
            "choose one, or omit it to have one generated.\n\n"
            "There is no self-service email reset — AsOne's sites are rural "
            "and mail delivery is not something the system can rely on."
        ),
    )
    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        user = self.get_object()

        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        password = services.set_user_password(
            user,
            new_password=serializer.validated_data.get("new_password"),
            must_change_password=serializer.validated_data["must_change_password"],
        )

        return Response(
            {
                "password": password,
                "detail": "Signed out of every session. Cannot be shown again.",
            }
        )

    @extend_schema(
        summary="Deactivate a user",
        request=None,
        responses={200: UserAdminSerializer},
        description=(
            "Removes access and signs the account out everywhere. The account "
            "and its history remain, because the ledger and audit trail point "
            "at it."
        ),
    )
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        self._guard_self(user, "deactivate")
        services.set_active(user, is_active=False)
        return Response(UserAdminSerializer(user).data)

    @extend_schema(summary="Reactivate a user", request=None, responses={200: UserAdminSerializer})
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        services.set_active(user, is_active=True)
        return Response(UserAdminSerializer(user).data)

    @extend_schema(
        summary="Sign a user out everywhere",
        request=None,
        responses={200: OpenApiResponse(description="Number of sessions retired.")},
        description=(
            "For a lost or stolen device, where the person still works here. "
            "The account stays usable; only its current sessions are retired."
        ),
    )
    @action(detail=True, methods=["post"], url_path="sign-out")
    def sign_out(self, request, pk=None):
        user = self.get_object()
        retired = services.force_sign_out(user)
        return Response({"sessions_retired": retired})


@extend_schema(
    tags=["User administration"],
    summary="Sign-in audit trail",
    description=(
        "Every sign-in attempt, successful or not, newest first. Failures are "
        "included: a run of them against one account is the visible sign that "
        "someone is guessing at it.\n\n"
        "Filter with `?email=`, `?succeeded=`, or `?user=`."
    ),
)
class LoginAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    """The audit trail. Read-only at every level — these rows are never edited."""

    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    queryset = LoginAttempt.objects.select_related("user")
    serializer_class = LoginAttemptSerializer
    filterset_fields = ["email", "succeeded", "user"]


@extend_schema(
    tags=["User administration"],
    summary="The roles, and what each one may do",
    responses=RoleSerializer(many=True),
    description=(
        "AsOne's five roles with the seven columns of their access matrix, "
        "the scope each reaches, and which site each requires.\n\n"
        "Built from the same permission classes the API enforces, so the "
        "navigation a client draws and the answer the server gives cannot "
        "drift apart. Use `requires_site` to decide whether a user-creation "
        "form shows a warehouse picker, a school picker, or neither."
    ),
)
class RoleListView(APIView):
    """The access matrix, published.

    Readable by any signed-in user. It describes AsOne's own access policy
    and contains no data about any person or site — a school clerk learning
    that Finance posts adjustments is not a disclosure.
    """

    permission_classes = AUTHENTICATED

    def get(self, request):
        return Response(RoleSerializer(services.role_catalogue(), many=True).data)
