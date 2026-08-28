"""Uniform Kits — bundles of SKUs sold as one unit.

AsOne's vocabulary: a Uniform Kit is what a school orders for a new student —
a fixed bundle of SKUs, e.g. a "PS Starter Kit" of one shirt, one tunic and a
pair of socks. The Point-of-Sale phase explodes a kit order into the SKUs
underneath it; this module only defines what is *in* a kit and what it costs.

**Kit price is never stored.** It is always the sum of the current prices of
the SKUs that make it up — see `compute_kit_price()` in catalog/services.py.
A stored total would go stale silently the moment one component's garment
was repriced, and nobody would notice until a school was invoiced the wrong
amount. Same reasoning as GarmentPrice; see catalog/models/pricing.py.
"""

from django.core.validators import MinValueValidator
from django.db import models


class Kit(models.Model):
    """A bundle of SKUs sold as one unit, e.g. a new-student starter kit.

    `kit_number` is entered by whoever creates the kit, unlike `Sku.number`.
    There is no requirement here for a system-assigned, never-reused
    sequence — kits are few (a handful, not the ~200 SKUs), and nothing in
    AsOne's brief calls for that guarantee at kit level.
    """

    class SchoolLevel(models.TextChoices):
        """Which school this kit is for.

        Deliberately two values, not three: unlike Garment.SchoolLevel,
        there is no "BOTH" here. AsOne's kits are described as school-level
        specific — a PS starter kit differs from an HS one — so there is no
        case yet for one kit appearing on both lists.
        """

        PRIMARY = "PS", "Primary School"
        HIGH = "HS", "High School"

    kit_number = models.CharField(
        max_length=20,
        unique=True,
        help_text='Entered by whoever creates the kit, e.g. "PS-STARTER-01".',
    )
    name = models.CharField(max_length=120, help_text='For example "PS Starter Kit".')
    school_level = models.CharField(max_length=2, choices=SchoolLevel.choices)

    # AsOne's "Active Y/N", same as Garment and Sku. Kits are deactivated,
    # never deleted — a school's past order may point at one.
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive kits stay in reports but cannot be ordered.",
    )

    class Meta:
        ordering = ["school_level", "name"]

    def __str__(self):
        return f"{self.kit_number} — {self.name}"


class KitItem(models.Model):
    """One line of a kit's bill of materials: a component SKU and how many."""

    # CASCADE, not this codebase's default PROTECT — see SETUP.md, where
    # PROTECT is the deliberate default everywhere, as a safety net against
    # accidentally losing data a delete did not mean to touch. This field is
    # the one legitimate exception: a KitItem has no meaning independent of
    # its Kit — it is a line on the Kit's own bill of materials, not a fact
    # about anything else — so when a Kit is deleted, its line items should
    # go with it automatically rather than blocking the delete or being left
    # behind as orphaned rows. Do not "fix" this back to PROTECT thinking it
    # was missed; it is intentional. Contrast with `sku` below, which keeps
    # PROTECT: a SKU is real master data that exists independently of any
    # one kit, so deleting one that a kit still depends on is refused,
    # exactly as it would be anywhere else in this codebase.
    kit = models.ForeignKey(Kit, on_delete=models.CASCADE, related_name="items")
    sku = models.ForeignKey(
        "catalog.Sku", on_delete=models.PROTECT, related_name="kit_items"
    )
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="How many of this SKU one kit contains.",
    )

    class Meta:
        ordering = ["kit", "sku"]
        constraints = [
            # One line per SKU per kit — two rows for the same SKU would
            # split its quantity across two line items instead of one.
            models.UniqueConstraint(fields=["kit", "sku"], name="unique_sku_per_kit")
        ]

    def __str__(self):
        return f"{self.quantity} × {self.sku.number} in {self.kit.kit_number}"
