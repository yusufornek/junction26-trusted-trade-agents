"""Exact pack conversion shared by ingestion, offers, quotes and fulfilment plans."""
from decimal import Decimal, ROUND_CEILING


def sales_step(product):
    """Organiser rule: every sales unit, including KG, is ordered whole."""
    return max(Decimal(1), Decimal(str(product.allowed_lot_size))).to_integral_value(rounding=ROUND_CEILING)


def pack_quantity(size, *, multiplier=1, factor=1, whole_case=False, requested=None, step=1):
    """Return six-decimal content, or the least sales quantity covering requested.

    Catalog CASE volume/netWeight already includes the sales-unit multiplier:
    whole_case=True applies only the unit conversion. BASE_UNIT_OR_EACH and
    name sizes need the multiplier once (a name's explicit count replaces it
    only when the catalog multiplier is one). Never multiply an existing CASE.
    All inputs use Decimal(str(value)); sizes are quantized before division.
    Explicit quote sales quantities pass through offer_for unchanged.
    """
    content = (Decimal(str(size)) * Decimal(str(factor))
               * (Decimal(1) if whole_case else Decimal(str(multiplier)))).quantize(Decimal("0.000001"))
    if requested is None:
        return content
    lot = Decimal(str(step))
    return (Decimal(str(requested)) / (content * lot)).to_integral_value(rounding=ROUND_CEILING) * lot
