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

from accounts.permissions import (
    AUTHENTICATED,
    CanEnterSchoolOrders,
    CanReceiveAndShip,
    scope_to_user_site,
)
from catalog.services import PriceNotSet

from . import services
from .models import SchoolOrder, SchoolOrderLine
from .serializers import (
    OrderAvailabilityRowSerializer,
    OrderDemandRowSerializer,
    SchoolOrderSerializer,
    SchoolOrderWriteSerializer,
)

#: F37, F38, F39 are warehouse fulfilment actions on an order, not the
#: School Orders Entry column the rest of this viewset belongs to — Bashir,
#: 30 August 2026: reuse CanReceiveAndShip rather than a new permission
#: class, since it already covers exactly this.
_WAREHOUSE_ACTIONS = frozenset({"availability", "pick_list", "pick"})


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

    `availability`, `pick_list` and `pick` (F37, F38, F39) are the warehouse
    fulfilment actions on the same order — gated by `CanReceiveAndShip`
    instead, via `get_permissions()`, since they are a different matrix
    column from everything else on this viewset.
    """

    permission_classes = [*AUTHENTICATED, CanEnterSchoolOrders]
    filterset_fields = ("status", "order_date")
    search_fields = ("number", "student_name")
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        # availability/pick_list/pick are warehouse fulfilment, not the
        # School Orders Entry column the rest of this viewset guards.
        if self.action in _WAREHOUSE_ACTIONS:
            return [permission() for permission in [*AUTHENTICATED, CanReceiveAndShip]]
        return super().get_permissions()

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
        # school_field for School Staff (their own school's orders);
        # warehouse_field for Warehouse Staff reaching availability/
        # pick_list/pick — without both, a warehouse clerk's requests would
        # pass CanReceiveAndShip and then find an empty queryset.
        return scope_to_user_site(
            queryset,
            self.request.user,
            school_field="school",
            warehouse_field="school__primary_warehouse",
        )

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

    @extend_schema(
        summary="Can the warehouse fill this order — F37",
        responses=OrderAvailabilityRowSerializer(many=True),
        description=(
            "One row per SKU: how many the order needs, how many are "
            "AVAILABLE at the order's warehouse, and the shortfall (0 means "
            "that line can be filled).\n\n"
            "Read-only — checking does not reserve anything. Post to "
            "`pick/` to actually reserve stock; that action refuses using "
            "this exact same comparison, so the two can never disagree."
        ),
    )
    @action(detail=True, methods=["get"])
    def availability(self, request, pk=None):
        order = self.get_object()
        rows = services.check_availability(order)
        return Response(OrderAvailabilityRowSerializer(rows, many=True).data)

    @extend_schema(
        summary="Pick list for the warehouse — F38",
        responses=OrderDemandRowSerializer(many=True),
        description=(
            "Same data as `demand/`, in the same description order — "
            "printed as the sheet a warehouse works from, reachable by "
            "warehouse roles rather than the school that placed the order."
        ),
    )
    @action(detail=True, methods=["get"], url_path="pick-list")
    def pick_list(self, request, pk=None):
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

    @extend_schema(
        summary="Pick this order — F39",
        request=None,
        responses={200: SchoolOrderSerializer},
        description=(
            "Reserves stock for every line: Available -> Pick. Total stock "
            "at the warehouse is unchanged; what changes is how much of it "
            "is still free to promise to a different order.\n\n"
            "Refused if the order is cancelled or already picked/shipped, "
            "or if any line is short — check `availability/` first."
        ),
    )
    @action(detail=True, methods=["post"])
    def pick(self, request, pk=None):
        order = self.get_object()

        try:
            services.pick_order(order, picked_by=request.user)
        except services.OrderCannotBePicked as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.OrderNotFillable as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        order.refresh_from_db()
        return Response(SchoolOrderSerializer(order).data)
