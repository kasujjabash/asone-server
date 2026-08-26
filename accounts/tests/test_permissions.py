"""The access matrix, asserted cell by cell.

This file is the executable copy of AsOne's access matrix: p.9 of the 14
August pack, plus the decisions in docs/CLIENT_DECISIONS.md that supersede
individual cells. Every departure from the printed grid is commented with the
decision that authorises it.

If AsOne changes a cell, this test fails until permissions.py is changed to
match — which is the point. Do not "fix" a failure here by editing the
expected grid without a written change from AsOne.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts import permissions as perms
from accounts.models import User
from catalog.models import School

from .factories import build_sites, make_user

Role = User.Role

#: The matrix. Rows are roles, columns are the seven functions, True where
#: AsOne's grid shows a scope and False where it shows a dash.
EXPECTED_MATRIX = {
    Role.PROGRAM_LEAD: {
        "table_updates": True,
        "production_orders": True,
        "warehouse_receiving_and_shipping": True,
        "inventory_adjustments": False,
        "school_orders": False,
        "backorder_transfers": True,
        "financial_reports": True,
    },
    Role.OPERATIONS_MANAGER: {
        "table_updates": True,
        "production_orders": True,
        "warehouse_receiving_and_shipping": True,
        "inventory_adjustments": False,
        "school_orders": False,
        "backorder_transfers": True,
        "financial_reports": True,
    },
    Role.WAREHOUSE_STAFF: {
        "table_updates": False,
        "production_orders": False,
        "warehouse_receiving_and_shipping": True,
        "inventory_adjustments": False,
        "school_orders": False,
        # Blank in the printed matrix; granted on Jim's 24 August direction.
        # See docs/CLIENT_DECISIONS.md D5.
        "backorder_transfers": True,
        "financial_reports": False,
    },
    Role.SCHOOL_STAFF: {
        "table_updates": False,
        "production_orders": False,
        "warehouse_receiving_and_shipping": False,
        "inventory_adjustments": False,
        "school_orders": True,
        "backorder_transfers": False,
        "financial_reports": False,
    },
    Role.FINANCE: {
        "table_updates": False,
        "production_orders": False,
        "warehouse_receiving_and_shipping": False,
        "inventory_adjustments": True,
        "school_orders": False,
        "backorder_transfers": False,
        "financial_reports": True,
    },
}


class AccessMatrixTests(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.users = {
            Role.PROGRAM_LEAD: make_user("sharon", Role.PROGRAM_LEAD),
            Role.OPERATIONS_MANAGER: make_user("andrew", Role.OPERATIONS_MANAGER),
            Role.WAREHOUSE_STAFF: make_user(
                "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
            ),
            Role.SCHOOL_STAFF: make_user(
                "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
            ),
            Role.FINANCE: make_user("musana", Role.FINANCE),
        }

    def test_every_cell_matches_the_client_matrix(self):
        from accounts.services import ACCESS_MATRIX_COLUMNS

        for role, expected_row in EXPECTED_MATRIX.items():
            user = self.users[role]
            for column, klass in ACCESS_MATRIX_COLUMNS.items():
                with self.subTest(role=role, column=column):
                    granted = klass().has_permission(_request_for(user), view=None)
                    self.assertEqual(granted, expected_row[column])

    def test_every_matrix_column_has_a_permission_class(self):
        """Guards against a column being added to one place and not the other."""
        from accounts.services import ACCESS_MATRIX_COLUMNS

        self.assertEqual(
            set(ACCESS_MATRIX_COLUMNS), set(EXPECTED_MATRIX[Role.PROGRAM_LEAD])
        )

    def test_a_deactivated_user_holds_nothing(self):
        lead = self.users[Role.PROGRAM_LEAD]
        lead.is_active = False

        self.assertFalse(perms.CanUpdateTables().has_permission(_request_for(lead), None))

    def test_an_anonymous_request_holds_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        request = _request_for(AnonymousUser())
        for klass in (perms.CanUpdateTables, perms.CanReceiveAndShip, perms.CanAdjustInventory):
            with self.subTest(permission=klass.__name__):
                self.assertFalse(klass().has_permission(request, None))


class ScopeToUserSiteTests(TestCase):
    """Row-level scoping, tested against School because it reaches both sites."""

    def setUp(self):
        self.sites = build_sites()

    def _schools_for(self, user):
        return scope_schools(user)

    def test_leads_see_every_site(self):
        for role in (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER):
            with self.subTest(role=role):
                user = make_user(f"lead-{role}", role)
                self.assertEqual(self._schools_for(user).count(), 2)

    def test_finance_sees_every_site(self):
        user = make_user("musana", Role.FINANCE)
        self.assertEqual(self._schools_for(user).count(), 2)

    def test_warehouse_staff_see_only_their_own_warehouse(self):
        user = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"])

        schools = self._schools_for(user)
        self.assertEqual([s.name for s in schools], ["Namayemba PS"])

    def test_school_staff_see_only_their_own_school(self):
        user = make_user("chrisis", Role.SCHOOL_STAFF, school=self.sites["school_b"])

        schools = self._schools_for(user)
        self.assertEqual([s.name for s in schools], ["Serere HS"])

    def test_a_misconfigured_user_sees_nothing_rather_than_everything(self):
        """Deny-by-default. A warehouse user with no warehouse is a broken
        account, and a broken account must not fall through to every site."""
        user = make_user("orphan", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"])
        user.warehouse = None  # bypassing clean(), as a bad data import would

        self.assertEqual(self._schools_for(user).count(), 0)

    def test_an_unusable_field_path_yields_nothing(self):
        """Warehouse staff querying a model with no warehouse path see nothing."""
        user = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"])

        scoped = perms.scope_to_user_site(School.objects.all(), user, school_field="pk")
        self.assertEqual(scoped.count(), 0)


class UserInvariantTests(TestCase):
    """User.clean() — role and site must agree, per AsOne's user table (p.3)."""

    def setUp(self):
        self.sites = build_sites()

    def test_warehouse_staff_must_have_a_warehouse(self):
        user = User(email="a@asone.test", role=Role.WAREHOUSE_STAFF)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean(exclude=["password"])
        self.assertIn("warehouse", cm.exception.error_dict)

    def test_warehouse_staff_cannot_also_hold_a_school(self):
        user = User(
            email="a@asone.test",
            role=Role.WAREHOUSE_STAFF,
            warehouse=self.sites["namayemba"],
            school=self.sites["school_a"],
        )
        with self.assertRaises(ValidationError) as cm:
            user.full_clean(exclude=["password"])
        self.assertIn("school", cm.exception.error_dict)

    def test_school_staff_must_have_a_school(self):
        user = User(email="a@asone.test", role=Role.SCHOOL_STAFF)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean(exclude=["password"])
        self.assertIn("school", cm.exception.error_dict)

    def test_all_location_roles_take_no_site(self):
        user = User(
            email="a@asone.test", role=Role.PROGRAM_LEAD, warehouse=self.sites["namayemba"]
        )
        with self.assertRaises(ValidationError) as cm:
            user.full_clean(exclude=["password"])
        self.assertIn("warehouse", cm.exception.error_dict)

    def test_a_correctly_configured_user_validates(self):
        user = User(
            email="a@asone.test", role=Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        user.full_clean(exclude=["password"])  # must not raise


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _request_for(user):
    """A stand-in request. The permission classes only read `.user`."""
    from types import SimpleNamespace

    return SimpleNamespace(user=user)


def scope_schools(user):
    """Scope the School table for `user`.

    School reaches a warehouse through `primary_warehouse` and is itself the
    school, which exercises both branches of scope_to_user_site().
    """
    return perms.scope_to_user_site(
        School.objects.order_by("name"),
        user,
        warehouse_field="primary_warehouse",
        school_field="pk",
    )
