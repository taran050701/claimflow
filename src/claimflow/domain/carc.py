"""Claim Adjustment Reason Codes (CARCs) used by ClaimFlow.

The standard list is maintained by X12; these are short versions of the
descriptions. Shared by validation, adjudication, remittance and explanations.
"""

CARC_DESCRIPTIONS = {
    "1": "Deductible amount",
    "2": "Coinsurance amount",
    "3": "Co-payment amount",
    "16": "Claim/service lacks information or has submission/billing error(s)",
    "18": "Exact duplicate claim/service",
    "26": "Expenses incurred prior to coverage",
    "27": "Expenses incurred after coverage terminated",
    "29": "The time limit for filing has expired",
    "31": "Patient cannot be identified as our insured",
    "45": "Charge exceeds fee schedule/maximum allowable",
    "96": "Non-covered charge(s)",
    "146": "Diagnosis was invalid for the date(s) of service reported",
    "151": "Information does not support this many/frequency of services",
    "181": "Procedure code was invalid on the date of service",
    "197": "Precertification/authorization absent",
    "206": "National Provider Identifier - missing",
    "207": "National Provider Identifier - invalid format",
    "208": "National Provider Identifier - not matched",
}


def describe(carc: str | None) -> str:
    return CARC_DESCRIPTIONS.get(carc or "", "")
