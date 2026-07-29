"""Price tier parsing, validation, and resolution.

Two responsibilities, deliberately separated:

1. **Shape detection and parsing.** Suppliers send quantity breaks in at least
   five incompatible shapes. Detecting which shape a file uses is a judgement
   call an agent can help with. Turning it into rows is not.

2. **Resolution.** Given an item, a quantity, and a date, return the price.
   This is pure arithmetic and belongs nowhere near a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Contract, PriceTier


# --------------------------------------------------------------------------
# Parsing supplier tier shapes
# --------------------------------------------------------------------------

# Matches wide-column headers: price_1_9, price_10_49, price_50_plus,
# qty_1_9_price, p1-9, and similar variants.
_WIDE_COLUMN = re.compile(
    r"^(?:price|p|qty|tier)[_\-]?(\d+)[_\-](?:(\d+)|plus|\+|up|over)(?:[_\-]?price)?$",
    re.IGNORECASE,
)

# Matches encoded strings: "1-9:42.00;10-49:38.50;50+:35.00"
_ENCODED = re.compile(r"(\d+)\s*-\s*(\d+|\+|plus)?\s*:\s*([\d.,]+)", re.IGNORECASE)


@dataclass
class ParsedTier:
    min_qty: int
    max_qty: int | None
    unit_price: Decimal
    price_unit: int = 1
    currency: str = "USD"


@dataclass
class LadderParseResult:
    shape: str
    tiers: list[ParsedTier] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def detect_wide_columns(headers: list[str]) -> list[tuple[str, int, int | None]]:
    """Find quantity-break columns in a wide-format header row.

    Returns (column_name, min_qty, max_qty) tuples, sorted by min_qty.
    """
    found: list[tuple[str, int, int | None]] = []
    for h in headers:
        m = _WIDE_COLUMN.match(h.strip())
        if not m:
            continue
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        found.append((h, lo, hi))
    return sorted(found, key=lambda t: t[1])


def parse_encoded_string(raw: str, currency: str = "USD") -> LadderParseResult:
    """Parse "1-9:42.00;10-49:38.50;50+:35.00" into tiers."""
    result = LadderParseResult(shape="encoded_string")
    matches = _ENCODED.findall(raw or "")
    if not matches:
        result.errors.append(f"No quantity breaks found in {raw!r}")
        return result

    for lo, hi, price in matches:
        max_qty = None if hi in ("", "+", "plus", None) else int(hi)
        result.tiers.append(
            ParsedTier(
                min_qty=int(lo),
                max_qty=max_qty,
                unit_price=Decimal(price.replace(",", "")),
                currency=currency,
            )
        )
    return result


def parse_wide_row(
    row: dict, tier_columns: list[tuple[str, int, int | None]],
    price_unit: int = 1, currency: str = "USD",
) -> LadderParseResult:
    """Build a ladder from one wide-format row."""
    result = LadderParseResult(shape="wide_columns")
    for col, lo, hi in tier_columns:
        raw = row.get(col)
        if raw in (None, "", "-"):
            continue
        try:
            price = Decimal(str(raw).replace(",", "").replace("$", "").strip())
        except Exception:
            result.errors.append(f"Column {col!r} is not a number: {raw!r}")
            continue
        result.tiers.append(
            ParsedTier(min_qty=lo, max_qty=hi, unit_price=price,
                       price_unit=price_unit, currency=currency)
        )
    return result


def apply_discount_ladder(
    base_price: Decimal, discounts: list[tuple[int, Decimal]],
    price_unit: int = 1, currency: str = "USD",
) -> LadderParseResult:
    """Turn (min_qty, discount_pct) pairs into absolute prices.

    The arithmetic happens here, in code. A model may identify that a file uses
    discount percentages; it never computes the resulting price.
    """
    result = LadderParseResult(shape="discount_pct")
    ordered = sorted(discounts, key=lambda d: d[0])
    for idx, (min_qty, pct) in enumerate(ordered):
        max_qty = ordered[idx + 1][0] - 1 if idx + 1 < len(ordered) else None
        price = (base_price * (Decimal(100) - pct) / Decimal(100)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        result.tiers.append(
            ParsedTier(min_qty=min_qty, max_qty=max_qty, unit_price=price,
                       price_unit=price_unit, currency=currency)
        )
    return result


# --------------------------------------------------------------------------
# Validation — hard failures, no confidence score overrides these
# --------------------------------------------------------------------------

def validate_ladder(
    tiers: list[ParsedTier],
    contract: Contract | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
) -> list[str]:
    """Return a list of errors. Empty list means the ladder may be published."""
    errors: list[str] = []
    if not tiers:
        return ["Ladder is empty"]

    ordered = sorted(tiers, key=lambda t: t.min_qty)

    if ordered[0].min_qty != 1:
        errors.append(f"Lowest tier starts at {ordered[0].min_qty}, expected 1")

    open_ended = [t for t in ordered if t.max_qty is None]
    if len(open_ended) != 1:
        errors.append(f"Expected exactly one open-ended top tier, found {len(open_ended)}")
    elif ordered[-1].max_qty is not None:
        errors.append("The open-ended tier is not the highest tier")

    for i in range(len(ordered) - 1):
        cur, nxt = ordered[i], ordered[i + 1]
        if cur.max_qty is None:
            continue
        if cur.max_qty + 1 != nxt.min_qty:
            errors.append(
                f"Gap or overlap between tier ending {cur.max_qty} "
                f"and tier starting {nxt.min_qty}"
            )

    # An increasing ladder is a parsing error in almost every real case.
    for i in range(len(ordered) - 1):
        if ordered[i + 1].unit_price > ordered[i].unit_price:
            errors.append(
                f"Price increases from {ordered[i].unit_price} to "
                f"{ordered[i + 1].unit_price} as quantity rises — "
                "almost certainly a parsing error"
            )
            break

    currencies = {t.currency for t in ordered}
    if len(currencies) > 1:
        errors.append(f"Mixed currencies in one ladder: {sorted(currencies)}")

    for t in ordered:
        if t.unit_price <= 0:
            errors.append(f"Non-positive price {t.unit_price} at qty {t.min_qty}")
        if t.price_unit < 1:
            errors.append(f"price_unit must be >= 1, got {t.price_unit}")

    if contract is not None:
        if currencies and contract.currency not in currencies:
            errors.append(
                f"Ladder currency {sorted(currencies)[0]} does not match "
                f"contract currency {contract.currency}"
            )
        if valid_from and valid_from < contract.valid_from:
            errors.append(
                f"Price validity starts {valid_from}, before contract start "
                f"{contract.valid_from}"
            )
        if valid_to and valid_to > contract.valid_to:
            errors.append(
                f"Price validity ends {valid_to}, after contract end {contract.valid_to}"
            )

    return errors


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

@dataclass
class ResolvedPrice:
    unit_price: Decimal
    price_unit: int
    currency: str
    effective_unit_price: Decimal   # unit_price / price_unit
    extended: Decimal               # effective_unit_price * quantity
    tier_id: str | None
    contract_id: str | None
    source: str                     # "tier" or "list"
    next_break: dict | None = None  # {"qty": 50, "unit_price": ...} if a cheaper tier is close


def resolve_price(
    db: Session,
    item,
    quantity: int,
    on: date | None = None,
    company_code: str | None = None,
) -> ResolvedPrice:
    """Resolve the price for a quantity on a date.

    Order of preference: highest-precedence valid contract tier, then any valid
    tier, then the item's list price.
    """
    on = on or date.today()
    quantity = max(1, int(quantity))

    tiers = db.scalars(
        select(PriceTier).where(PriceTier.item_id == item.id)
    ).all()

    candidates = [t for t in tiers if t.covers_qty(quantity) and t.valid_on(on)]

    if candidates:
        contract_ids = {t.contract_id for t in candidates if t.contract_id}
        precedence: dict[str, int] = {}
        if contract_ids:
            for c in db.scalars(select(Contract).where(Contract.id.in_(contract_ids))).all():
                precedence[c.id] = c.precedence if c.is_valid_on(on) else -1

        def rank(t: PriceTier) -> tuple[int, Decimal]:
            p = precedence.get(t.contract_id, 0) if t.contract_id else 0
            return (p, -Decimal(str(t.unit_price)))

        best = sorted(candidates, key=rank, reverse=True)[0]

        unit_price = Decimal(str(best.unit_price))
        price_unit = max(1, best.price_unit or 1)
        effective = (unit_price / price_unit).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        return ResolvedPrice(
            unit_price=unit_price,
            price_unit=price_unit,
            currency=best.currency,
            effective_unit_price=effective,
            extended=(effective * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            tier_id=best.id,
            contract_id=best.contract_id,
            source="tier",
            next_break=_next_break(tiers, quantity, on, effective),
        )

    # Fall back to list price.
    list_price = Decimal(str(item.list_price or 0))
    price_unit = max(1, item.price_unit or 1)
    effective = (list_price / price_unit).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return ResolvedPrice(
        unit_price=list_price,
        price_unit=price_unit,
        currency=item.currency,
        effective_unit_price=effective,
        extended=(effective * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        tier_id=None,
        contract_id=None,
        source="list",
        next_break=_next_break(tiers, quantity, on, effective),
    )


def _next_break(
    tiers: list[PriceTier], quantity: int, on: date, current_effective: Decimal
) -> dict | None:
    """The next quantity break that would lower the unit price.

    Surfacing this in the storefront matters: after an OCI punchout returns,
    changing quantity in SAP does not re-resolve the price. Users need to pick
    the right quantity before they come back.
    """
    upcoming = [
        t for t in tiers
        if t.valid_on(on) and t.min_qty > quantity
        and (Decimal(str(t.unit_price)) / max(1, t.price_unit or 1)) < current_effective
    ]
    if not upcoming:
        return None
    nxt = min(upcoming, key=lambda t: t.min_qty)
    eff = (Decimal(str(nxt.unit_price)) / max(1, nxt.price_unit or 1)).quantize(Decimal("0.0001"))
    return {
        "qty": nxt.min_qty,
        "unit_price": float(eff),
        "saving_per_unit": float(current_effective - eff),
    }
