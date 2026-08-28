"""The Group Order — F16.

AsOne's definition (p.2):

    "The consolidated uniform requirements total across all 3 TCs. Used to
    initially fund the TCs."

One group order covers every Tailoring Center, and the production orders
placed on individual TCs should sum back up to it.

OPEN QUESTION Q14 — the pack contradicts itself. The definition above says
consolidated across all three TCs, but the header layout on p.4 gives a Group
Order a single `TC` and `Ship to Warehouse`, which would make it
indistinguishable from a production order and would make "production orders
sum up to the group order" meaningless.

Modelled to the definition: no TC, no warehouse. If AsOne confirms the p.4
header instead, this gains two nullable foreign keys — a small change, which
is why the more useful reading was taken now rather than waiting.

OPEN QUESTION Q11 — whether the Group Order stays relevant beyond the initial
capital phase is still with the client. Nothing here assumes it does:
production orders can exist without one.
"""

from django.db import models

from .base import OrderDocument, OrderLine


class GroupOrder(OrderDocument):
    """The consolidated requirement across all Tailoring Centers."""

    class Meta(OrderDocument.Meta):
        abstract = False
        verbose_name = "group order"

    def save(self, *args, **kwargs):
        # Assigned here rather than in a signal, so it is visible where it
        # happens, and only when missing — the number is printed on documents
        # and must never change.
        if not self.number:
            from procurement.services import next_group_order_number

            self.number = next_group_order_number()
        super().save(*args, **kwargs)


class GroupOrderLine(OrderLine):
    """One SKU on a group order."""

    # CASCADE, not PROTECT — one of only two deliberate exceptions in this
    # codebase. A line has no meaning apart from its order, and removing an
    # order should not require emptying it by hand first.
    order = models.ForeignKey(
        GroupOrder, on_delete=models.CASCADE, related_name="lines"
    )

    class Meta(OrderLine.Meta):
        abstract = False
        constraints = [
            # Two rows for the same SKU would make the order total a question
            # rather than a fact, and would break reconciliation against the
            # production orders.
            models.UniqueConstraint(
                fields=["order", "sku"], name="unique_sku_per_group_order"
            )
        ]
