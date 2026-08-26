"""The published role catalogue.

Its whole value is that it cannot disagree with what the server enforces, so
that is what these tests check — not the literal contents, which
test_permissions.py already pins against AsOne's matrix.
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts import permissions as perms
from accounts.models import User
from accounts.services import ACCESS_MATRIX_COLUMNS, role_catalogue

from .factories import build_sites, make_user


class RoleCatalogueTests(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )

    def test_requires_authentication(self):
        self.assertEqual(
            self.client.get(reverse("accounts:roles")).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_any_signed_in_user_may_read_it(self):
        """It is AsOne's access policy, not anyone's data."""
        self.client.force_authenticate(self.clerk)
        response = self.client.get(reverse("accounts:roles"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(User.Role))

    def test_it_lists_every_role_exactly_once(self):
        values = [row["value"] for row in role_catalogue()]

        self.assertEqual(sorted(values), sorted(r.value for r in User.Role))
        self.assertEqual(len(values), len(set(values)))

    def test_every_row_covers_all_seven_matrix_columns(self):
        for row in role_catalogue():
            with self.subTest(role=row["value"]):
                self.assertEqual(set(row["functions"]), set(ACCESS_MATRIX_COLUMNS))

    def test_the_catalogue_agrees_with_what_the_server_enforces(self):
        """The point of the endpoint. If these ever diverge, it is a lie."""
        for row in role_catalogue():
            # An unsaved User is enough: has_role() reads role, is_active
            # and is_authenticated, and the last is always True on a real
            # User instance.
            user = User(role=row["value"], is_active=True)

            for column, klass in ACCESS_MATRIX_COLUMNS.items():
                with self.subTest(role=row["value"], column=column):
                    self.assertEqual(
                        row["functions"][column],
                        perms.has_role(user, *klass.roles),
                    )

    def test_requires_site_matches_the_model_invariant(self):
        """The form shows a picker exactly when User.clean() demands one."""
        for row in role_catalogue():
            with self.subTest(role=row["value"]):
                self.assertEqual(
                    row["requires_site"], User.required_site_field(row["value"])
                )

    def test_site_roles_ask_for_a_site_and_all_location_roles_do_not(self):
        by_value = {row["value"]: row for row in role_catalogue()}

        self.assertEqual(by_value[User.Role.WAREHOUSE_STAFF]["requires_site"], "warehouse")
        self.assertEqual(by_value[User.Role.SCHOOL_STAFF]["requires_site"], "school")
        for role in User.ALL_SITE_ROLES:
            self.assertIsNone(by_value[role]["requires_site"])

    def test_scope_matches_the_signed_in_user_access_summary(self):
        """A role's published scope and a user's own reported scope must agree."""
        self.client.force_authenticate(self.clerk)
        mine = self.client.get(reverse("accounts:me")).data["access"]["scope"]

        catalogue = {row["value"]: row["scope"] for row in role_catalogue()}
        self.assertEqual(mine, catalogue[User.Role.WAREHOUSE_STAFF])
