"""Serializers for the authentication endpoints.

Serialization only. No business logic lives here — see accounts/services.py.
The one thing these classes do carry is *shape*: which fields a client may
read and, more importantly, which it may write.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from catalog.models import School, Warehouse

from .models import LoginAttempt, User
from .services import access_summary


# ---------------------------------------------------------------------------
# Sites, as they appear inside a user
# ---------------------------------------------------------------------------
# Deliberately thin. The full site endpoints belong to the catalog app; these
# exist so the React app can label "Namayemba" without a second request.


class WarehouseSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ("id", "name")
        read_only_fields = fields


class SchoolSummarySerializer(serializers.ModelSerializer):
    level_display = serializers.CharField(source="get_level_display", read_only=True)

    class Meta:
        model = School
        fields = ("id", "name", "level", "level_display")
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Reading a user
# ---------------------------------------------------------------------------


class UserSerializer(serializers.ModelSerializer):
    """The current user, as returned by login and by GET /api/auth/me/.

    Every field is read-only. Role and site are set by an administrator in
    the Django admin; nothing a signed-in user sends can change them.
    """

    role_display = serializers.CharField(source="get_role_display", read_only=True)
    warehouse = WarehouseSummarySerializer(read_only=True)
    school = SchoolSummarySerializer(read_only=True)
    access = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "role_display",
            "warehouse",
            "school",
            "is_active",
            "must_change_password",
            "last_login",
            "access",
        )
        read_only_fields = fields

    def get_access(self, obj) -> dict:
        """Which matrix columns this user holds — for drawing the navigation.

        Advisory only. The server re-checks on every request regardless of
        what the client chose to display.
        """
        return access_summary(obj)


# ---------------------------------------------------------------------------
# Writing a user — only the parts they own
# ---------------------------------------------------------------------------


class MeUpdateSerializer(serializers.ModelSerializer):
    """PATCH /api/auth/me/ — a user editing their own contact details.

    SECURITY: the field list is an allow-list, not a convenience. `role`,
    `warehouse`, `school`, `is_active`, `is_staff`, `is_superuser` and
    `password` are all absent, so a school clerk cannot PATCH themselves into
    Finance or reassign themselves to another site. Anything added to this
    tuple becomes self-service — add nothing without meaning to.
    """

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")

    def validate_email(self, value):
        """Reject an address already in use.

        AbstractUser does not make email unique at the database level, so this
        is a courtesy check rather than a guarantee — two simultaneous saves
        could still collide. It is here to give a clear message, not to
        enforce an invariant.
        """
        value = value.strip()
        if not value:
            return value
        clash = User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("That email address is already in use.")
        return value


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class LoginSerializer(TokenObtainPairSerializer):
    """Username and password in, access + refresh + the user record out.

    No custom claims are packed into the token. The role travels in the
    response body instead, because a claim inside a JWT is a snapshot: revoke
    someone's access at 10:00 and a token minted at 09:59 still asserts the
    old role until it expires. The server therefore reads role from the
    database on every request, and the frontend gets its copy from here.

    Inactive accounts are rejected by Django's own authentication backend, so
    deactivating a user is enough to lock them out at the next login.
    """

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class LoginChallengeIssuedSerializer(serializers.Serializer):
    """What the password step returns now: a challenge, not tokens."""

    challenge = serializers.UUIDField(
        help_text="Send this back with the code to finish signing in."
    )
    expires_at = serializers.DateTimeField()
    detail = serializers.CharField()
    email_hint = serializers.CharField(
        help_text='Where the code went, partly masked — "j••••s@asone.test".'
    )


class VerifyLoginCodeSerializer(serializers.Serializer):
    """POST /api/auth/login/verify/ — the second factor."""

    challenge = serializers.UUIDField()
    code = serializers.CharField(
        max_length=12,
        trim_whitespace=True,
        help_text="The code from the email.",
    )


class EmailVerificationSerializer(serializers.Serializer):
    """Confirming a new account's address with the emailed code."""

    email = serializers.EmailField()
    code = serializers.CharField(max_length=12, trim_whitespace=True)


class LogoutSerializer(serializers.Serializer):
    """POST /api/auth/logout/ — the refresh token to blacklist."""

    refresh = serializers.CharField(write_only=True)


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


class PasswordChangeSerializer(serializers.Serializer):
    """POST /api/auth/password/change/.

    The current password is required even though the request is already
    authenticated. A stolen access token is then not enough to lock the real
    owner out of their own account.
    """

    current_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    new_password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("That is not your current password.")
        return value

    def validate_new_password(self, value):
        """Run Django's configured password validators.

        Passing the user lets UserAttributeSimilarityValidator reject a
        password that is just their email address or name.
        """
        try:
            validate_password(value, user=self.context["request"].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def validate(self, attrs):
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "The new password must be different from the current one."}
            )
        return attrs


# ---------------------------------------------------------------------------
# Administering other people's accounts
# ---------------------------------------------------------------------------


class UserAdminSerializer(serializers.ModelSerializer):
    """A user as a lead sees them in the user management screens.

    Distinct from UserSerializer, which is a person looking at themselves.
    This one exposes the administrative fields — role, site, active — as
    *writable*, which is precisely why it is only ever reachable behind
    CanUpdateTables.
    """

    role_display = serializers.CharField(source="get_role_display", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True, default=None)
    school_name = serializers.CharField(source="school.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "role_display",
            "warehouse",
            "warehouse_name",
            "school",
            "school_name",
            "is_active",
            "must_change_password",
            "last_login",
            "date_joined",
        )
        # SECURITY: `is_staff`, `is_superuser` and `password` are absent from
        # `fields` entirely, so no request body can reach them. Django admin
        # access and password setting are deliberately not API operations.
        read_only_fields = (
            "id",
            "must_change_password",
            "last_login",
            "date_joined",
        )

    def validate(self, attrs):
        """Enforce the role/site invariant that User.clean() owns.

        Serializer validation does not call Model.clean(), so without this a
        school user could be given a warehouse through the API even though
        the admin refuses it. Rather than restate the rule, build the object
        the save would produce and ask the model.
        """
        instance = self.instance
        candidate = User(
            **{
                "email": attrs.get("email", getattr(instance, "email", "")),
                "role": attrs.get("role", getattr(instance, "role", "")),
                "warehouse": attrs.get("warehouse", getattr(instance, "warehouse", None)),
                "school": attrs.get("school", getattr(instance, "school", None)),
            }
        )
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc

        return attrs


class UserCreateSerializer(UserAdminSerializer):
    """Creating an account: first name, last name, email, role.

    The password is generated unless one is typed, and shown to the lead
    **once** so they can pass it on. It is never emailed — the confirmation
    code is, and keeping the two on separate routes is what makes the code
    worth anything.

    `must_change_password` stays on, because until the owner replaces it two
    people know that password.
    """

    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
        help_text=(
            "Leave this out and one is generated for you, shown once. Type "
            "one only if you have a reason to. Either way you pass it to the "
            "person yourself — it is never emailed."
        ),
    )
    must_change_password = serializers.BooleanField(
        default=True,
        help_text=(
            "Require the user to choose their own password at first sign-in. "
            "Leave on unless you have a reason not to — until they do, you "
            "know their password too."
        ),
    )

    class Meta(UserAdminSerializer.Meta):
        fields = (
            "id",
            "first_name",
            "last_name",
            "email",
            "role",
            "warehouse",
            "school",
            "password",
            "must_change_password",
        )
        read_only_fields = ("id",)
        extra_kwargs = {
            "email": {"required": True},
            "role": {"required": True},
            "first_name": {"required": True, "allow_blank": False},
            "last_name": {"required": True, "allow_blank": False},
        }

    def validate_password(self, value):
        """Run Django's configured validators on a lead-chosen password.

        Without this a lead could set "1234" for a colleague, and the rules
        that apply when someone chooses their own password would not apply
        when someone else chose it for them.
        """
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class SetPasswordSerializer(serializers.Serializer):
    """A lead setting another person's password.

    Used for "they forgot it" and "this account needs a new one". The current
    password is not required — the whole point is that nobody has it.
    """

    new_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
        help_text="Omit to have a password generated and returned once.",
    )
    must_change_password = serializers.BooleanField(
        default=True,
        help_text="Require the user to replace it at their next sign-in.",
    )

    def validate_new_password(self, value):
        # Blank means "generate one" — the validators apply to a chosen
        # password, not to the absence of one.
        if not value:
            return value
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class LoginAttemptSerializer(serializers.ModelSerializer):
    """One row of the sign-in audit trail. Read-only, always."""

    class Meta:
        model = LoginAttempt
        fields = ("id", "email", "user", "succeeded", "ip_address", "user_agent", "at")
        read_only_fields = fields


class RoleSerializer(serializers.Serializer):
    """One role in the published catalogue.

    Not backed by a model — roles are a TextChoices on User, deliberately.
    They are a fixed part of AsOne's operating model, not data their staff
    add and remove, and the access matrix is enforced in code. This
    serializer exists so the shape is documented in /api/docs/ rather than
    being an undescribed blob.
    """

    value = serializers.CharField(help_text='Store this on the user, e.g. "WAREHOUSE_STAFF".')
    label = serializers.CharField(help_text="Show this to a person.")
    summary = serializers.CharField(help_text="What the role is for, in AsOne's words.")
    caveat = serializers.CharField(help_text="A notable limit worth showing when picking a role.")
    scope = serializers.ChoiceField(
        choices=["all_locations", "assigned_warehouse", "assigned_schools", "none"],
        help_text="How wide this role reaches.",
    )
    requires_site = serializers.CharField(
        allow_null=True,
        help_text='"warehouse", "school", or null. Which site picker to show.',
    )
    functions = serializers.DictField(
        child=serializers.BooleanField(),
        help_text="The seven columns of AsOne's access matrix.",
    )
