"""HTTP layer for master data.

Views translate between HTTP and the rest of the system and nothing else.
Anything worth testing lives in catalog/services.py.

Every viewset here declares two things:

    permission_classes   who may write — always the leads
    read_roles           who may read — different for each table

The split comes straight from AsOne's access matrix, where editing is one
column but "view only" is granted table by table. See
`accounts.permissions.MasterDataAccess`.
"""

from datetime import date

from django.db import transaction
from django.db.models import Count
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import MasterDataAccess

from . import services
from .models import (
    Garment,
    GarmentPrice,
    MinimumStockLevel,
    School,
    Size,
    Sku,
    TailoringCenter,
    Warehouse,
)
from .serializers import (
    GarmentPriceSerializer,
    GarmentSerializer,
    MinimumStockLevelSerializer,
    PriceListRowSerializer,
    RepriceSerializer,
    SchoolSerializer,
    SizeSerializer,
    SkuSerializer,
    TailoringCenterSerializer,
    WarehouseSerializer,
)

Role = User.Role
MASTER_DATA = [IsAuthenticated, MasterDataAccess]


def _requested_date(request) -> date:
    """Read `?on=YYYY-MM-DD`, defaulting to today.

    Prices are dated, so nearly every read here can be asked "as at when".

    A malformed date is the caller's mistake, so it is a 400. Left unhandled,
    `fromisoformat` raises ValueError and DRF reports a 500 — which sends
    someone hunting a server fault that is not there.
    """
    raw = request.query_params.get("on")
    if not raw:
        return date.today()

    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError(
            {"on": f"'{raw}' is not a date. Use YYYY-MM-DD."}
        ) from None


def _requested_level(request, *, required=True):
    """Read `?level=PS|HS`.

    Validated rather than passed through. An unrecognised level used to match
    nothing but the BOTH garments, so a typo returned a short price list with
    a 200 — a wrong answer is worse than an error, because nobody checks it.
    """
    raw = request.query_params.get("level")
    if not raw:
        if required:
            return School.Level.PRIMARY
        return None

    valid = {choice.value for choice in School.Level}
    if raw not in valid:
        raise DRFValidationError(
            {"level": f"'{raw}' is not a school level. Use {' or '.join(sorted(valid))}."}
        )
    return raw


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — sites"])
class TailoringCenterViewSet(viewsets.ModelViewSet):
    """Where uniforms are made. Not system users — they exist so production
    orders and receipts have something to point at."""

    queryset = TailoringCenter.objects.all().order_by("name")
    serializer_class = TailoringCenterSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF,)
    search_fields = ("name",)


@extend_schema(tags=["Master data — sites"])
class WarehouseViewSet(viewsets.ModelViewSet):
    """Where finished stock is held."""

    queryset = Warehouse.objects.select_related("primary_tailoring_center").order_by("name")
    serializer_class = WarehouseSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF,)
    filterset_fields = ("primary_tailoring_center",)


@extend_schema(tags=["Master data — sites"])
class SchoolViewSet(viewsets.ModelViewSet):
    """The customers. Each orders from one primary warehouse."""

    queryset = School.objects.select_related("primary_warehouse").order_by("name")
    serializer_class = SchoolSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF)
    filterset_fields = ("level", "primary_warehouse")


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — products"])
class SizeViewSet(viewsets.ModelViewSet):
    """Garment sizes, shared across garments so "10" means one thing."""

    queryset = Size.objects.all()
    serializer_class = SizeSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)


@extend_schema(tags=["Master data — products"])
class GarmentViewSet(viewsets.ModelViewSet):
    """Uniform components, before a size is chosen.

    Price lives here rather than on the SKU, so `current_price` is a real
    field of a garment and `POST /garments/{id}/reprice/` is how it changes.
    """

    serializer_class = GarmentSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("school_level", "is_active")
    search_fields = ("name", "colour")

    def get_queryset(self):
        # Annotated rather than looked up per row: without this, listing 45
        # garments costs 46 queries.
        return services.with_current_price(
            Garment.objects.annotate(sku_count=Count("skus"))
        ).order_by("name")

    @extend_schema(
        summary="Change a garment's price",
        request=RepriceSerializer,
        responses={201: GarmentPriceSerializer},
        description=(
            "Closes the current open-ended price on `active_from` and opens a "
            "new one.\n\n"
            "This is the only sanctioned way to change a price. Editing a "
            "price row in place would rewrite history, and an invoice "
            "reprinted next term would no longer match the original."
        ),
    )
    @action(detail=True, methods=["post"])
    def reprice(self, request, pk=None):
        garment = self.get_object()

        serializer = RepriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Two writes — closing the old price and opening the new one. A
        # failure between them would leave the garment unpriced.
        with transaction.atomic():
            price = services.reprice(
                garment,
                serializer.validated_data["unit_price"],
                serializer.validated_data["active_from"],
            )

        return Response(
            GarmentPriceSerializer(price).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="This garment's price history",
        responses=GarmentPriceSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def prices(self, request, pk=None):
        garment = self.get_object()
        return Response(
            GarmentPriceSerializer(garment.prices.order_by("-active_date"), many=True).data
        )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — pricing"])
class GarmentPriceViewSet(viewsets.ModelViewSet):
    """The pricing table, across all garments.

    Day-to-day changes should go through `POST /garments/{id}/reprice/`, which
    closes the previous price for you. This viewset is for corrections and
    auditing.
    """

    queryset = GarmentPrice.objects.select_related("garment").order_by(
        "garment__name", "-active_date"
    )
    serializer_class = GarmentPriceSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("garment",)


@extend_schema(
    tags=["Master data — pricing"],
    summary="Price list for Primary or High School",
    parameters=[
        OpenApiParameter("level", str, description="`PS` or `HS`.", required=True),
        OpenApiParameter("on", str, description="Date, `YYYY-MM-DD`. Defaults to today."),
    ],
    responses=PriceListRowSerializer(many=True),
    description=(
        "Active garments on that price list with their price on that date, "
        "ordered by name. Garments marked `BOTH` appear on each list.\n\n"
        "Garments with no price on the date are **omitted, not shown at "
        "zero** — a price list is a document a school orders from, and a line "
        "with no price is worse than no line. Use `/price-lists/gaps/` to "
        "find them before publishing."
    ),
)
class PriceListView(APIView):
    """F15 — generate price lists at garment level."""

    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)

    def get(self, request):
        level = _requested_level(request)
        rows = services.price_list(level, _requested_date(request))
        return Response(PriceListRowSerializer(rows, many=True).data)


@extend_schema(
    tags=["Master data — pricing"],
    summary="Active garments with no price",
    parameters=[
        OpenApiParameter("on", str, description="Date, `YYYY-MM-DD`. Defaults to today."),
        OpenApiParameter("level", str, description="Limit to `PS` or `HS`."),
    ],
    responses=GarmentSerializer(many=True),
    description=(
        "The gap report behind a price list. Run it before publishing one, or "
        "a garment silently disappears from what the schools can order."
    ),
)
class PriceGapView(APIView):
    permission_classes = MASTER_DATA
    read_roles = (Role.FINANCE,)

    def get(self, request):
        gaps = services.garments_without_a_price(
            _requested_date(request), _requested_level(request, required=False)
        )
        return Response(GarmentSerializer(gaps, many=True).data)


# ---------------------------------------------------------------------------
# SKUs
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — SKUs"])
class SkuViewSet(viewsets.ModelViewSet):
    """One garment in one size. What is counted, ordered and picked.

    The control number is assigned by the system and is read-only: it is
    printed on pick lists and packing lists, and must mean the same thing
    forever.
    """

    serializer_class = SkuSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("garment", "size", "is_active", "garment__school_level")
    search_fields = ("number", "description")

    def get_queryset(self):
        # A SKU's price is its garment's price, so the subquery correlates on
        # garment_id. 200 SKUs in one query rather than 201.
        return services.with_current_price(
            Sku.objects.select_related("garment", "size"),
            garment_field="garment_id",
        ).order_by("description")


@extend_schema(tags=["Master data — SKUs"])
class MinimumStockLevelViewSet(viewsets.ModelViewSet):
    """The level that triggers a replenishment order, per SKU per warehouse."""

    queryset = MinimumStockLevel.objects.select_related(
        "sku", "sku__garment", "warehouse"
    ).order_by("warehouse__name", "sku__description")
    serializer_class = MinimumStockLevelSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF,)
    filterset_fields = ("warehouse", "sku")
