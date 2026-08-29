"""Site and product names are unique regardless of case.

A plain `unique=True` on a CharField is case-SENSITIVE in Postgres. That is
not a theoretical gap — the development database ended up with:

    Warehouse  "Namayemba"  and  "Namayemba warehouse"
    School     "Namayemba Primary School"  and  "Namayemba primary school"

and the constraint accepted both. AsOne staff type these names, so two
spellings of one warehouse would split its stock in a way no later count
could reconcile.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import Garment, Kit, School, Size, TailoringCenter, Warehouse


class NamesAreCaseInsensitive(TestCase):
    def setUp(self):
        self.tc = TailoringCenter.objects.create(name="Idudi")
        self.warehouse = Warehouse.objects.create(name="Namayemba")

    def assertRefuses(self, create):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create()

    def test_a_tailoring_center_cannot_repeat_in_another_case(self):
        self.assertRefuses(lambda: TailoringCenter.objects.create(name="idudi"))

    def test_a_warehouse_cannot_repeat_in_another_case(self):
        self.assertRefuses(lambda: Warehouse.objects.create(name="NAMAYEMBA"))

    def test_a_school_cannot_repeat_in_another_case(self):
        """The exact pair that got into the development database."""
        School.objects.create(
            name="Namayemba Primary School",
            level=School.Level.PRIMARY,
            primary_warehouse=self.warehouse,
        )
        self.assertRefuses(
            lambda: School.objects.create(
                name="Namayemba primary school",
                level=School.Level.PRIMARY,
                primary_warehouse=self.warehouse,
            )
        )

    def test_a_size_cannot_repeat_in_another_case(self):
        Size.objects.create(name="S", sort_order=1)
        self.assertRefuses(lambda: Size.objects.create(name="s", sort_order=2))

    def test_a_kit_number_cannot_repeat_in_another_case(self):
        """Kit numbers are typed by hand, so this is the likeliest to happen."""
        Kit.objects.create(
            kit_number="PS-STARTER-01", name="Starter", school_level=Kit.SchoolLevel.PRIMARY
        )
        self.assertRefuses(
            lambda: Kit.objects.create(
                kit_number="ps-starter-01",
                name="Duplicate",
                school_level=Kit.SchoolLevel.PRIMARY,
            )
        )

    def test_a_garment_cannot_repeat_in_another_case_at_the_same_level(self):
        Garment.objects.create(name="White Shirt", school_level=Garment.SchoolLevel.PRIMARY)
        self.assertRefuses(
            lambda: Garment.objects.create(
                name="white shirt", school_level=Garment.SchoolLevel.PRIMARY
            )
        )

    def test_the_same_garment_name_at_a_different_level_is_still_allowed(self):
        """A PS white shirt and an HS white shirt are two garments, and may
        carry different prices."""
        Garment.objects.create(name="White Shirt", school_level=Garment.SchoolLevel.PRIMARY)
        Garment.objects.create(name="White Shirt", school_level=Garment.SchoolLevel.HIGH)

        self.assertEqual(Garment.objects.filter(name="White Shirt").count(), 2)


class GenuinelyDifferentNamesStillWork(TestCase):
    """The constraint must not be so eager that it blocks real data."""

    def test_two_different_warehouses_are_fine(self):
        Warehouse.objects.create(name="Namayemba")
        Warehouse.objects.create(name="Serere")

        self.assertEqual(Warehouse.objects.count(), 2)

    def test_names_that_merely_share_a_prefix_are_fine(self):
        """"Namayemba" and "Namayemba warehouse" are different strings.

        This is the case the constraint does NOT catch, and cannot — telling
        those apart needs a person. Recorded here so nobody assumes the
        constraint solves duplicate sites in general.
        """
        Warehouse.objects.create(name="Namayemba")
        Warehouse.objects.create(name="Namayemba warehouse")

        self.assertEqual(Warehouse.objects.count(), 2)
