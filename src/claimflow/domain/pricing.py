"""Line pricing: allowed amount -> payer paid vs patient responsibility.

Pure functions over Decimal. The order of operations is a documented business
rule (real plans differ; this is ours):

1. allowed      = lesser of (fee schedule rate x units, billed)
2. copay plans  : patient pays the flat copay (never more than allowed); no deductible
   otherwise    : patient pays remaining deductible first, then coinsurance on the rest
3. out-of-pocket max caps the patient's total for the year; coinsurance is
   reduced first, then deductible/copay
4. payer paid   = allowed - patient responsibility

Coinsurance is the only step that can produce fractions of a cent. It's rounded
half-up to the cent once, and the payer gets the exact remainder, so
allowed == paid + patient always holds.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Accumulators:
    """How much the member has already paid toward this plan year's limits."""

    deductible_met: Decimal
    out_of_pocket_met: Decimal


@dataclass(frozen=True)
class PlanTerms:
    annual_deductible: Decimal
    coinsurance_rate: Decimal  # 0.200 = patient pays 20%
    out_of_pocket_max: Decimal


@dataclass(frozen=True)
class LinePricing:
    billed: Decimal
    allowed: Decimal
    contractual_adjustment: Decimal  # billed - allowed; provider writes it off (CO-45)
    deductible: Decimal
    copay: Decimal
    coinsurance: Decimal
    patient_responsibility: Decimal
    paid: Decimal


def price_line(
    *,
    billed: Decimal,
    rate_per_unit: Decimal,
    units: int,
    copay: Decimal | None,
    terms: PlanTerms,
    acc: Accumulators,
) -> tuple[LinePricing, Accumulators]:
    """Price one line. Returns the pricing and the accumulators AFTER this line,
    which the next line must use."""
    allowed = min(rate_per_unit * units, billed)

    if copay is not None:
        copay_amt = min(copay, allowed)
        deductible_amt = ZERO
        coinsurance_amt = ZERO
    else:
        copay_amt = ZERO
        deductible_remaining = max(terms.annual_deductible - acc.deductible_met, ZERO)
        deductible_amt = min(allowed, deductible_remaining)
        coinsurance_amt = ((allowed - deductible_amt) * terms.coinsurance_rate).quantize(
            CENT, rounding=ROUND_HALF_UP
        )

    # Out-of-pocket max: cap the patient's share, trimming coinsurance first.
    oop_remaining = max(terms.out_of_pocket_max - acc.out_of_pocket_met, ZERO)
    excess = max(deductible_amt + copay_amt + coinsurance_amt - oop_remaining, ZERO)
    trim = min(excess, coinsurance_amt)
    coinsurance_amt -= trim
    excess -= trim
    if copay is not None:
        copay_amt -= excess
    else:
        deductible_amt -= excess

    patient = deductible_amt + copay_amt + coinsurance_amt
    pricing = LinePricing(
        billed=billed,
        allowed=allowed,
        contractual_adjustment=billed - allowed,
        deductible=deductible_amt,
        copay=copay_amt,
        coinsurance=coinsurance_amt,
        patient_responsibility=patient,
        paid=allowed - patient,
    )
    new_acc = Accumulators(
        deductible_met=acc.deductible_met + deductible_amt,
        out_of_pocket_met=acc.out_of_pocket_met + patient,
    )
    return pricing, new_acc
