"""Unit tests for line pricing. Every test also checks the money reconciles."""

from decimal import Decimal as D

from claimflow.domain.pricing import Accumulators, LinePricing, PlanTerms, price_line

GOLD = PlanTerms(
    annual_deductible=D("500.00"), coinsurance_rate=D("0.200"), out_of_pocket_max=D("4000.00")
)


def price(billed, rate, *, units=1, copay=None, terms=GOLD, ded_met="0", oop_met="0"):
    return price_line(
        billed=D(billed),
        rate_per_unit=D(rate),
        units=units,
        copay=D(copay) if copay is not None else None,
        terms=terms,
        acc=Accumulators(D(ded_met), D(oop_met)),
    )


def assert_reconciles(p: LinePricing):
    assert p.billed == p.allowed + p.contractual_adjustment
    assert p.allowed == p.paid + p.patient_responsibility
    assert p.patient_responsibility == p.deductible + p.copay + p.coinsurance


def test_mri_example_from_the_lessons():
    """Billed 1500, allowed 600, $250 deductible left, 20% coinsurance."""
    p, acc = price("1500.00", "600.00", ded_met="250.00", oop_met="250.00")

    assert p.allowed == D("600.00")
    assert p.contractual_adjustment == D("900.00")  # CO-45, provider writes it off
    assert p.deductible == D("250.00")
    assert p.coinsurance == D("70.00")
    assert p.patient_responsibility == D("320.00")
    assert p.paid == D("280.00")
    assert acc == Accumulators(D("500.00"), D("570.00"))
    assert_reconciles(p)


def test_allowed_is_lesser_of_rate_and_billed():
    p, _ = price("50.00", "90.00", copay="25.00")
    assert p.allowed == D("50.00")
    assert p.contractual_adjustment == D("0.00")
    assert_reconciles(p)


def test_rate_is_per_unit():
    p, _ = price("100.00", "20.00", units=2, ded_met="500.00")
    assert p.allowed == D("40.00")
    assert_reconciles(p)


def test_copay_replaces_deductible_and_coinsurance():
    p, acc = price("150.00", "90.00", copay="25.00")
    assert (p.copay, p.deductible, p.coinsurance) == (D("25.00"), D("0.00"), D("0.00"))
    assert p.paid == D("65.00")
    assert acc.deductible_met == D("0")  # copays don't count toward the deductible...
    assert acc.out_of_pocket_met == D("25.00")  # ...but do count toward out-of-pocket
    assert_reconciles(p)


def test_copay_never_exceeds_allowed():
    p, _ = price("20.00", "20.00", copay="25.00")
    assert p.copay == D("20.00")
    assert p.paid == D("0.00")
    assert_reconciles(p)


def test_deductible_not_met_patient_pays_everything():
    p, _ = price("200.00", "100.00")
    assert p.deductible == D("100.00")
    assert p.paid == D("0.00")  # approved, but the payer owes $0
    assert_reconciles(p)


def test_deductible_already_met_only_coinsurance():
    p, _ = price("200.00", "100.00", ded_met="500.00")
    assert (p.deductible, p.coinsurance, p.paid) == (D("0.00"), D("20.00"), D("80.00"))
    assert_reconciles(p)


def test_out_of_pocket_max_caps_patient_share():
    # $30 left before OOP max; the patient would otherwise owe $70.
    p, acc = price("600.00", "600.00", ded_met="500.00", oop_met="3970.00")
    assert p.patient_responsibility == D("30.00")
    assert p.paid == D("570.00")
    assert acc.out_of_pocket_met == D("4000.00")
    assert_reconciles(p)


def test_after_out_of_pocket_max_payer_pays_100_percent():
    p, _ = price("300.00", "300.00", ded_met="500.00", oop_met="4000.00")
    assert p.patient_responsibility == D("0.00")
    assert p.paid == D("300.00")
    assert_reconciles(p)


def test_coinsurance_rounds_half_up_and_payer_gets_remainder():
    # 20% of 33.33 = 6.666 -> 6.67; payer gets 26.66, not a rounded value.
    p, _ = price("33.33", "33.33", ded_met="500.00")
    assert p.coinsurance == D("6.67")
    assert p.paid == D("26.66")
    assert_reconciles(p)
