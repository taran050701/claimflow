"""Unit tests for explain(): "why was this claim decided this way?" from audit records."""

import uuid

from claimflow.domain.explanation import AuditRecord, explain

RUN_1, RUN_2, VALIDATION = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CODES = {1: "99213", 2: "72148"}


def rec(
    seq,
    event_type,
    *,
    run=RUN_1,
    rule=None,
    line=None,
    outcome=None,
    carc=None,
    to_status=None,
    message="",
    inputs=None,
):
    return AuditRecord(
        seq,
        event_type,
        to_status,
        rule,
        line,
        outcome,
        carc,
        run,
        {"message": message, "inputs": inputs or {}},
    )


def rule(seq, name, line, outcome="PASS", carc=None, run=RUN_1, **kw):
    return rec(
        seq, "RULE_EVALUATED", run=run, rule=name, line=line, outcome=outcome, carc=carc, **kw
    )


def adjudicated(seq, run=RUN_1, status="ADJUDICATED"):
    event = "CLAIM_ADJUDICATED" if status == "ADJUDICATED" else "CLAIM_PENDED"
    return rec(seq, event, run=run, to_status=status)


LINE_1_APPROVED = [
    rule(2, "Coverage", 1),
    rule(3, "CodeEdits", 1),
    rule(4, "PriorAuth", 1),
    rule(5, "Duplicate", 1),
    rule(6, "Pricing", 1, message="Allowed 90.00"),
]


def test_line_denied_by_prior_auth_shows_rule_reason_and_inputs():
    records = [
        rule(1, "Eligibility", None),
        *LINE_1_APPROVED,
        rule(7, "Coverage", 2),
        rule(8, "CodeEdits", 2),
        rule(
            9,
            "PriorAuth",
            2,
            "DENY",
            "197",
            message="Required prior authorization is absent",
            inputs={"required": True, "auths_on_file": []},
        ),
        adjudicated(10),
    ]
    e = explain("DENIED", CODES, records)

    visit, mri = e.lines
    assert (visit.outcome, visit.decided_by) == ("APPROVED", "Pricing")
    assert (mri.outcome, mri.decided_by, mri.reason_code) == ("DENIED", "PriorAuth", "197")
    assert mri.reason == "Precertification/authorization absent"
    assert mri.inputs == {"required": True, "auths_on_file": []}
    assert mri.rules_passed == ("Coverage", "CodeEdits")
    assert e.summary == (
        "DENIED: 1 approved, 1 denied; line 2 (72148) denied by PriorAuth: "
        "CO-197 Precertification/authorization absent"
    )


def test_claim_level_eligibility_denial_explains_every_line():
    records = [
        rule(1, "Eligibility", None, "DENY", "27", message="Coverage terminated"),
        adjudicated(2),
    ]
    e = explain("DENIED", CODES, records)
    assert {(li.outcome, li.decided_by, li.reason_code) for li in e.lines} == {
        ("DENIED", "Eligibility", "27")
    }
    assert all(li.rules_passed == () for li in e.lines)


def test_uses_only_the_latest_adjudication_run():
    first_run = [
        rule(1, "Eligibility", None),
        rule(2, "Coverage", 1),
        rule(3, "PriorAuth", 1, "PEND"),
        adjudicated(4, status="PENDED"),
    ]
    second_run = [
        rule(5, "Eligibility", None, run=RUN_2),
        *[replace_run(r, RUN_2, 6 + i) for i, r in enumerate(LINE_1_APPROVED)],
        adjudicated(11, run=RUN_2),
    ]
    e = explain("ADJUDICATED", {1: "99213"}, first_run + second_run)
    assert [(li.outcome, li.decided_by) for li in e.lines] == [("APPROVED", "Pricing")]


def replace_run(r: AuditRecord, run: uuid.UUID, seq: int) -> AuditRecord:
    return AuditRecord(
        seq,
        r.event_type,
        r.to_status,
        r.rule_name,
        r.line_number,
        r.outcome,
        r.reason_code,
        run,
        r.details,
    )


def test_rejected_claim_lists_validation_failures():
    records = [
        rec(1, "CLAIM_RECEIVED", run=uuid.uuid4(), to_status="RECEIVED"),
        rec(
            2,
            "VALIDATION_EDIT_FAILED",
            run=VALIDATION,
            rule="MEMBER_NOT_FOUND",
            carc="31",
            outcome="FAIL",
            message="Member M9999 cannot be identified",
        ),
        rec(
            3,
            "VALIDATION_EDIT_FAILED",
            run=VALIDATION,
            rule="INVALID_CPT",
            line=2,
            carc="181",
            outcome="FAIL",
        ),
        rec(4, "CLAIM_REJECTED", run=VALIDATION, to_status="REJECTED"),
    ]
    e = explain("REJECTED", CODES, records)
    assert e.lines == ()
    assert [(f.edit_code, f.line_number, f.reason_code) for f in e.validation_failures] == [
        ("MEMBER_NOT_FOUND", None, "31"),
        ("INVALID_CPT", 2, "181"),
    ]
    assert e.validation_failures[0].reason == "Patient cannot be identified as our insured"
    assert e.summary == "Rejected in validation (2 issue(s): MEMBER_NOT_FOUND, INVALID_CPT)"


def test_pended_line_is_explained_as_pended():
    records = [
        rule(1, "Eligibility", None),
        rule(2, "Coverage", 2),
        rule(3, "CodeEdits", 2),
        rule(4, "PriorAuth", 2, "PEND", message="Authorization request is still under review"),
        adjudicated(5, status="PENDED"),
    ]
    e = explain("PENDED", {2: "72148"}, records)
    assert (e.lines[0].outcome, e.lines[0].decided_by) == ("PENDED", "PriorAuth")
    assert e.summary == "PENDED: 1 pended"


def test_nothing_decided_yet():
    e = explain("RECEIVED", CODES, [rec(1, "CLAIM_RECEIVED", to_status="RECEIVED")])
    assert e.summary == "No adjudication decision has been made yet"
    assert e.lines == () and e.validation_failures == ()
