"""HTTP layer for the stock ledger.

Read-only throughout. Stock changes as a consequence of a document being
posted — a receipt today, an adjustment or a shipment later — never because
a client asked for a movement directly. There is deliberately no endpoint
that writes here.

Everything is scoped by warehouse: a Namayemba clerk sees Namayemba's stock
and nothing else, per AsOne's matrix.
"""

from datetime import date

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import (
    AUTHENTICATED,
    CanReceiveAndShip,
    MasterDataAccess,
    scope_to_user_site,
)
from catalog.models import Warehouse

from . import services
from .models import ReasonCode, StockMovement
from .serializers import (
    ReasonCodeSerializer,
    ReorderAlertSerializer,
    StockLevelSerializer,
    StockMovementSerializer,
)

Role = User.Role


def _as_of(request):
    """Read `?as_of=YYYY-MM-DD`. A count sheet from last Friday is checked
    against what the system thought last Friday, not against today."""
    raw = request.query_params.get("as_of")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError(
            {"as_of": f"'{raw}' is not a date. Use YYYY-MM-DD."}
        ) from None


def _warehouse(request):
    """Read `?warehouse=<id>`, if given."""
    raw = request.query_params.get("warehouse")
    if not raw:
        return None
    return get_object_or_404(Warehouse.objects.all(), pk=raw)


@extend_schema(
    tags=["Inventory"],
    summary="Stock levels",
    parameters=[
        OpenApiParameter("warehouse", int, description="Limit to one warehouse."),
        OpenApiParameter("as_of", str, description="Level as at a date, YYYY-MM-DD."),
        OpenApiParameter("include_zero", bool, description="Include SKUs at zero."),
    ],
    responses=StockLevelSerializer(many=True),
    description=(
        "F47 — units on hand per SKU per warehouse.\n\n"
        "There is no stock-level table. Every figure here is summed from the "
        "ledger on read, which is what makes the audit trail and the stock "
        "level incapable of disagreeing.\n\n"
        "Warehouse staff see their own warehouse only."
    ),
)
class StockLevelView(APIView):
    # F47 gives Finance "All sites" — they post the inventory adjustments and
    # cannot do that without seeing what is on hand. CanReceiveAndShip does
    # not include Finance, so the view declares its own read audience.
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)

    def get(self, request):
        warehouse = _warehouse(request)

        # Warehouse staff are pinned to their own site whatever they ask for.
        if request.user.role == Role.WAREHOUSE_STAFF:
            warehouse = request.user.warehouse

        rows = services.stock_levels(
            warehouse=warehouse,
            as_of=_as_of(request),
            include_zero=request.query_params.get("include_zero") == "true",
        )
        return Response(StockLevelSerializer(rows, many=True).data)


@extend_schema(
    tags=["Inventory"],
    summary="Below-minimum reorder alerts",
    parameters=[
        OpenApiParameter("warehouse", int, description="Limit to one warehouse."),
        OpenApiParameter("as_of", str, description="As at a date, YYYY-MM-DD."),
    ],
    responses=ReorderAlertSerializer(many=True),
    description=(
        "F50 — SKUs at or below the minimum level set for that warehouse, "
        "which is what should trigger a replenishment order on the Tailoring "
        "Centers.\n\n"
        "A SKU with a floor set but no stock at all is included: zero is "
        "below any positive minimum, and that is exactly the case worth "
        "alerting on."
    ),
)
class ReorderAlertView(APIView):
    # F50 leaves the Finance cell blank, unlike F47 next door. Reordering is
    # an operational decision, not a financial one.
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF,)

    def get(self, request):
        warehouse = _warehouse(request)
        if request.user.role == Role.WAREHOUSE_STAFF:
            warehouse = request.user.warehouse

        alerts = services.below_minimum(warehouse=warehouse, as_of=_as_of(request))
        return Response(ReorderAlertSerializer(alerts, many=True).data)


@extend_schema(tags=["Inventory"])
class StockMovementViewSet(viewsets.ReadOnlyModelViewSet):
    """The ledger itself — F48, the audit trail by SKU.

    Read-only, and not by omission: `StockMovement` refuses to be updated or
    deleted at model level too. Filter with `?sku=` for one product's whole
    history.
    """

    serializer_class = StockMovementSerializer
    # F48 gives Finance "All sites" on the audit trail.
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)
    filterset_fields = ("sku", "warehouse", "movement_type", "document_number")

    def get_queryset(self):
        queryset = StockMovement.objects.select_related(
            "sku", "sku__garment", "warehouse", "created_by"
        )
        return scope_to_user_site(
            queryset, self.request.user, warehouse_field="warehouse"
        )


@extend_schema(tags=["Inventory"])
class ReasonCodeViewSet(viewsets.ModelViewSet):
    """Why an inventory adjustment was made — F13.

    Master data, so the leads maintain it and Finance reads it. Warehouse and
    school staff have no access: the matrix gives the Inventory Adj column to
    Finance alone, and the reason codes belong to that column.

    **No DELETE.** A code is retired with `is_active`, because past
    adjustments point at it — an audit trail that cannot say why a movement
    happened is not an audit trail. Phase 2 will add that foreign key.
    """

    queryset = ReasonCode.objects.all()
    serializer_class = ReasonCodeSerializer
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.FINANCE,)
    filterset_fields = ("is_active",)
    search_fields = ("code", "name")
    http_method_names = ["get", "post", "patch", "head", "options"]
