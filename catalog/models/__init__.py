"""Master data — the tables Central Office owns.

Split by subject rather than kept in one file, so a change to pricing does not
mean scrolling past the site tables.

    sites.py     Tailoring Centers, Warehouses, Schools
    products.py  Garment, Size
    skus.py      Sku, MinimumStockLevel
    pricing.py   time-effective garment prices

Everything is re-exported here, so the rest of the project imports from
`catalog.models` and never has to know which file a model lives in.
"""

from .pricing import GarmentPrice
from .products import Garment, Size
from .sites import School, TailoringCenter, Warehouse
from .skus import MinimumStockLevel, Sku

__all__ = [
    "Garment",
    "GarmentPrice",
    "MinimumStockLevel",
    "School",
    "Size",
    "Sku",
    "TailoringCenter",
    "Warehouse",
]
