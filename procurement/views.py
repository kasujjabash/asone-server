"""HTTP layer for ordering from the Tailoring Centers.

Two permission shapes appear here, and the difference is the whole point of
AsOne's matrix:

    Group orders      leads write, Finance reads. No warehouse involvement.
    Production orders leads write; warehouse staff work with **their own
                      warehouse's** orders; Finance reads all of them.

The second needs `scope_to_user_site()` as well as a permission class. A
permission class lets a Namayemba clerk open the production orders screen;
only scoping stops them reading Serere's.
"""

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import (
    CanEnterProductionOrders,
    MasterDataAccess,
    scope_to_user_site,
)

from . import services
from .models import GroupOrder, GroupOrderLine, ProductionOrder, ProductionOrderLine
from .models.base import OrderStatus
from .serializers import (
    GroupOrderSerializer,
    OrderAmendSerializer,
    GroupOrderWriteSerializer,
    ProductionOrderSerializer,
    ProductionOrderWriteSerializer,
    ReconciliationRowSerializer,
)

Role = User.Role


class OrderViewSetMixin:
    """Shared behaviour for the two order documents.

    Orders are **never deleted**. They are a financial record — a group order
    funds the Tailoring Centers, and a production order is a commitment to
    one. Cancel by setting the status; the number then stays retired, which
    is the point of drawing it from a sequence.
    """

    http_method_names = ["get", "post", "patch", "head", "options"]

    #: Set by each viewset. Used to render the response after a write, so the
    #: client gets the order number and totals rather than an echo of what it
    #: sent — the write serializer has no `number`, because the system
    #: assigns it.
    read_serializer_class = None

    def create(self, request, *args, **kwargs):
        """Create through the service, respond with the full document.

        DRF would render the response with the *write* serializer, which
        omits `number`, `id` and the saved lines. A client would then have to
        re-fetch just to learn what it had created.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        order = self.create_order(serializer.validated_data)

        return Response(
            self.read_serializer_class(order).data, status=status.HTTP_201_CREATED
        )

    def create_order(self, data):
        """Raise the order. Implemented per document type."""
        raise NotImplementedError

    def partial_update(self, request, *args, **kwargs):
        """Amend the header. Lines are F18 and are not editable yet."""
        order = self.get_object()

        serializer = OrderAmendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        for field, value in serializer.validated_data.items():
            setattr(order, field, value)
        order.save(update_fields=[*serializer.validated_data, "status"])

        return Response(self.read_serializer_class(order).data)


@extend_schema(tags=["Procurement — group orders"])
class GroupOrderViewSet(OrderViewSetMixin, viewsets.ModelViewSet):
    """F16 — the consolidated requirement across all Tailoring Centers.

    Leads write; Finance reads (the matrix gives them costed reports on group
    orders). Warehouse and school staff have no access at all.
    """

    permission_classes = [IsAuthenticated, MasterDataAccess]
    read_roles = (Role.FINANCE,)
    filterset_fields = ("status", "order_date")

    def get_queryset(self):
        return GroupOrder.objects.select_related("created_by").prefetch_related(
            Prefetch(
                "lines",
                queryset=GroupOrderLine.objects.select_related("sku", "sku__garment"),
            )
        )

    read_serializer_class = GroupOrderSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return GroupOrderWriteSerializer
        if self.action == "partial_update":
            return OrderAmendSerializer
        return GroupOrderSerializer

    @extend_schema(
        summary="Raise a group order",
        request=GroupOrderWriteSerializer,
        responses={201: GroupOrderSerializer},
        description=(
            "Header and lines in one request, written in one transaction.\n\n"
            "Leave `unit_price` off a line and the garment's price on the "
            "order date is copied onto it and fixed there. A later reprice "
            "does not restate what this order was worth."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def create_order(self, data):
        return services.create_group_order(created_by=self.request.user, **data)

    @extend_schema(
        summary="Group order against its production orders",
        responses=ReconciliationRowSerializer(many=True),
        description=(
            "AsOne's rule is that production orders \"should initially sum up "
            "to the Group Order\".\n\n"
            "Reported, not enforced: a warehouse ordering a little extra for "
            "safety stock is a judgement call for a person, not an error for "
            "the system. A negative `difference` means the Tailoring Centers "
            "have been asked for less than the requirement — the case worth "
            "chasing. Cancelled production orders are excluded."
        ),
    )
    @action(detail=True, methods=["get"])
    def reconciliation(self, request, pk=None):
        rows = services.reconcile(self.get_object())
        return Response(ReconciliationRowSerializer(rows, many=True).data)


@extend_schema(tags=["Procurement — production orders"])
class ProductionOrderViewSet(OrderViewSetMixin, viewsets.ModelViewSet):
    """F17 — one warehouse's order on one Tailoring Center.

    A warehouse may order from any TC, not only its primary one (p.4), so the
    Tailoring Center is chosen per order.
    """

    permission_classes = [IsAuthenticated, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)
    filterset_fields = ("status", "tailoring_center", "warehouse", "group_order")

    def get_queryset(self):
        queryset = ProductionOrder.objects.select_related(
            "tailoring_center", "warehouse", "group_order", "created_by"
        ).prefetch_related(
            Prefetch(
                "lines",
                queryset=ProductionOrderLine.objects.select_related(
                    "sku", "sku__garment"
                ),
            )
        )
        # A permission class opens the screen; only this stops a Namayemba
        # clerk reading Serere's orders.
        return scope_to_user_site(queryset, self.request.user, warehouse_field="warehouse")

    read_serializer_class = ProductionOrderSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return ProductionOrderWriteSerializer
        if self.action == "partial_update":
            return OrderAmendSerializer
        return ProductionOrderSerializer

    @extend_schema(
        summary="Raise a production order",
        request=ProductionOrderWriteSerializer,
        responses={201: ProductionOrderSerializer},
        description=(
            "Header and lines in one request, written in one transaction.\n\n"
            "`group_order` is optional: the first season's orders break down "
            "a group order, but reorders and emergency orders later in the "
            "year have none."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def create_order(self, data):
        return services.create_production_order(created_by=self.request.user, **data)


@extend_schema(
    tags=["Procurement — production orders"],
    summary="Open production orders",
    responses=ProductionOrderSerializer(many=True),
    description=(
        "F22 — orders placed on the Tailoring Centers but not yet closed.\n\n"
        "Warehouse staff see their own warehouse only. Receipts are not built "
        "yet, so \"open\" currently means the status is Open; once receipts "
        "exist this should also cover orders fully received but not closed."
    ),
)
class OpenProductionOrderView(APIView):
    permission_classes = [IsAuthenticated, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)

    def get(self, request):
        queryset = scope_to_user_site(
            ProductionOrder.objects.select_related(
                "tailoring_center", "warehouse", "group_order", "created_by"
            ).prefetch_related("lines__sku"),
            request.user,
            warehouse_field="warehouse",
        )
        orders = services.open_production_orders(queryset)
        return Response(ProductionOrderSerializer(orders, many=True).data)
