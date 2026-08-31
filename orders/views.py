"""HTTP layer for school orders.

The point of sale is the one part of this system where the *school* writes.
Everywhere else they read. So this is also the one place where site scoping
does real work on writes as well as reads: a school clerk sees their own
school's orders and can only create orders for that school.
"""

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from accounts.permissions import AUTHENTICATED, CanEnterSchoolOrders, scope_to_user_site
from catalog.services import PriceNotSet

from . import services
from .models import SchoolOrder, SchoolOrderLine
from .serializers import (
    OrderDemandRowSerializer,
    SchoolOrderSerializer,
    SchoolOrderWriteSerializer,
)


@extend_schema(tags=["Orders — point of sale"])
class SchoolOrderViewSet(viewsets.ModelViewSet):
    """A student's uniform order — F30, F31, F32, F33.

    **School staff only.** AsOne's matrix leaves the School Orders Entry
    column blank for both leads, and the Role Access sheet omits the point of
    sale from their screens — see `CanEnterSchoolOrders`, and open question
    Q7, which asks whether schools have the computers to do this at all.

    Scoped to the clerk's own school in both directions: they see their
    school's orders, and an order they create belongs to that school
    whatever the request body says.

    **Orders are never deleted.** A school hands the number to a parent as
    an invoice, so the document has to survive. Cancelling is the way out —
    which is F36 and not built yet.
    """

    permission_classes = [*AUTHENTICATED, CanEnterSchoolOrders]
    filterset_fields = ("status", "order_date")
    search_fields = ("number", "student_name")
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = SchoolOrder.objects.select_related(
            "school", "school__primary_warehouse", "created_by"
        ).prefetch_related(
            Prefetch(
                "lines",
                queryset=SchoolOrderLine.objects.select_related(
                    "sku", "sku__garment", "from_kit"
                ),
            )
        )
        return scope_to_user_site(queryset, self.request.user, school_field="school")

    def get_serializer_class(self):
        return SchoolOrderWriteSerializer if self.action == "create" else SchoolOrderSerializer

    @extend_schema(
        summary="Place an order",
        request=SchoolOrderWriteSerializer,
        responses={201: SchoolOrderSerializer},
        description=(
            "A school orders at **kit or item level**, or both — send `kits`, "
            "`skus`, or each. At least one line between them.\n\n"
            "Every kit is exploded into its component SKUs on the way in, "
            "because the warehouse picks garments and never \"a kit\". Those "
            "components are stored: editing the kit's bill of materials next "
            "term will not change an order already placed.\n\n"
            "Lines carry the price on the order date, so a reprinted invoice "
            "still adds up.\n\n"
            "The order lands on **Hold** and stays there — releasing it needs "
            "payment confirmed, and what confirms payment is still an open "
            "question with AsOne.\n\n"
            "`school` is not accepted: the order belongs to the clerk's own "
            "school. Nothing retired can be ordered, and a school can only "
            "order from its own level."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if request.user.school is None:
            raise PermissionDenied(
                "Your account is not attached to a school, so it cannot place orders."
            )

        data = dict(serializer.validated_data)

        try:
            order = services.place_order(
                school=request.user.school,
                created_by=request.user,
                kits=data.pop("kits"),
                skus=data.pop("skus"),
                **data,
            )
        except services.EmptyOrder as exc:
            raise DRFValidationError({"skus": str(exc)}) from exc
        except services.InactiveItem as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.WrongSchoolLevel as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except PriceNotSet as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        return Response(SchoolOrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="What the warehouse has to pick",
        responses=OrderDemandRowSerializer(many=True),
        description=(
            "One row per SKU, however many places it came from.\n\n"
            "An order's lines are kept split by source so the invoice can "
            "show the school which items came from the kit it chose. A pick "
            "list does not care — two shirts in a kit plus one ordered loose "
            "is three shirts off the same shelf."
        ),
    )
    @action(detail=True, methods=["get"])
    def demand(self, request, pk=None):
        order = self.get_object()
        rows = [
            {
                "sku_number": sku.number,
                "sku_description": sku.description,
                "quantity": quantity,
            }
            for sku, quantity in services.order_demand(order)
        ]
        return Response(OrderDemandRowSerializer(rows, many=True).data)
