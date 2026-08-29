"""Shared serializer validators.

Framework plumbing, like config/exceptions.py — no business rules. Lives here
rather than in an app because more than one app needs it.
"""

from rest_framework.validators import UniqueValidator, qs_filter


class CaseInsensitiveUniqueValidator(UniqueValidator):
    """Reject a value that already exists in any capitalisation.

    Needed because DRF cannot see it otherwise. `ModelSerializer` builds its
    uniqueness validators from `unique=True` on a field — but the names in
    this project are guarded by *functional* constraints
    (`UniqueConstraint(Lower("name"))`), which DRF has no way to inspect.

    Without this, a duplicate reaches the database and comes back as an
    IntegrityError, which DRF reports as a **500**. The rule is enforced
    either way; the difference is whether the client is told "that name is
    taken" or "the server broke".

    Instance exclusion on update comes free from UniqueValidator, so editing
    a row does not trip over its own value.

        name = serializers.CharField(
            validators=[CaseInsensitiveUniqueValidator(queryset=Warehouse.objects.all())]
        )
    """

    message = "There is already one with this name. Names are not case-sensitive."

    def filter_queryset(self, value, queryset, field_name):
        return qs_filter(queryset, **{f"{field_name}__iexact": value})
