"""Evidence faults: who is blamed, what is final, and what only ever delays."""

import pytest

from tests.direct.support import (
    CAPS, DEPOSIT, PROMPT_HEAD, RETURN_BY, STAKE, Case, analysis, at, check_books, claim_of,
    credit, deploy, digest, loan_of, review, serve, settle, to_review,
)

REVIEW_AT = RETURN_BY + 60
VERDICT = analysis(["damaged", "damaged"])


def _setup(direct_vm, direct_deploy, actors, name="ev", claimed=(2, 4), accepted=(), **case_kwargs):
    contract = deploy(direct_vm, direct_deploy)
    case = Case(name, actors[0], actors[1], verdict=VERDICT, **case_kwargs)
    to_review(contract, direct_vm, case, list(claimed), list(accepted))
    return contract, case


def _strike(contract, direct_vm, case, times, start=REVIEW_AT):
    moment = start
    for _ in range(times):
        direct_vm.warp(at(moment))
        review(contract, direct_vm, case)
        moment = claim_of(contract, case)["retry_at"]
    return moment


# -------------------------------------------------- unavailable: owner side
@pytest.mark.parametrize("which", ["inventory", "owner_report"])
@pytest.mark.parametrize("status", [404, 429, 500, 503, 301, 100])
def test_owner_side_refusals_are_retryable_and_never_final(
    direct_vm, direct_deploy, actors, which, status
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    url = case.inv_url if which == "inventory" else case.owner_url
    direct_vm.mock_web(url, {"status": status, "body": b"nope"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["reason"] == "owner_unavailable" and c["owner_misses"] == 1
    assert c["ruling"] == "" and c["review_calls"] == 1 and c["attempts"] == 0
    assert loan_of(contract, case)["status"] == "claimed"


def test_an_outage_that_heals_lets_the_review_continue(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.owner_url, {"status": 503, "body": b"down"})
    review(contract, direct_vm, case)
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": case.owner_report})
    direct_vm.warp(at(REVIEW_AT + 60))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "ruled" and c["owner_misses"] == 1 and c["reason"] == ""


def test_four_owner_side_outages_void_the_disputed_items_but_return_the_stake(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors, claimed=(2, 4, 5), accepted=(5,))
    direct_vm.mock_web(case.inv_url, {"status": 503, "body": b"down"})
    _strike(contract, direct_vm, case, 3)
    assert claim_of(contract, case)["ruling"] == ""
    _strike(contract, direct_vm, case, 1, start=claim_of(contract, case)["retry_at"])
    c = claim_of(contract, case)
    assert c["ruling"] == "owner_evidence_unavailable" and c["owner_misses"] == 4
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["deducted_mask"] == 0b10000 and loan["deducted_wei"] == CAPS[4]
    assert loan["stake_forfeited_wei"] == 0
    assert credit(contract, case.owner) == CAPS[4] + STAKE
    assert credit(contract, case.holder) == DEPOSIT - CAPS[4]
    check_books(contract, actors, [case])


def test_a_missing_web_mock_counts_as_an_unavailable_source(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.clear_mocks()  # nothing is served at all
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["reason"] == "owner_unavailable"


# ------------------------------------------------- unavailable: holder side
def test_a_holder_outage_is_retryable_and_counts_against_the_holder_only(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.holder_url, {"status": 503, "body": b"down"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["reason"] == "holder_unavailable"
    assert (c["holder_misses"], c["owner_misses"]) == (1, 0)
    assert c["ruling"] == ""


def test_four_holder_outages_lose_the_disputed_items_to_the_owner(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, claimed=(2, 4, 5), accepted=(5,))
    direct_vm.mock_web(case.holder_url, {"status": 500, "body": b"down"})
    _strike(contract, direct_vm, case, 4)
    c = claim_of(contract, case)
    assert c["ruling"] == "holder_evidence_unavailable" and c["ruled_mask"] == 0b01010
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["deducted_wei"] == CAPS[1] + CAPS[3] + CAPS[4]
    assert credit(contract, case.holder) == DEPOSIT - CAPS[1] - CAPS[3] - CAPS[4]
    assert credit(contract, case.owner) == CAPS[1] + CAPS[3] + CAPS[4] + STAKE
    check_books(contract, actors, [case])


def test_holder_outage_never_masks_an_owner_side_outage(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.inv_url, {"status": 503, "body": b"down"})
    direct_vm.mock_web(case.holder_url, {"status": 503, "body": b"down"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    # The owner's documents are fetched first, so the owner is the one blamed.
    assert (c["owner_misses"], c["holder_misses"]) == (1, 0)


def test_outages_on_both_sides_accumulate_separately(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.holder_url, {"status": 503, "body": b"down"})
    moment = _strike(contract, direct_vm, case, 2)
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": case.holder_report})
    direct_vm.mock_web(case.owner_url, {"status": 503, "body": b"down"})
    _strike(contract, direct_vm, case, 2, start=moment)
    c = claim_of(contract, case)
    assert (c["holder_misses"], c["owner_misses"]) == (2, 2) and c["ruling"] == ""


# -------------------------------------------------------------- tampering
def test_a_tampered_owner_report_forfeits_the_stake_and_voids_the_disputed_items(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors, claimed=(2, 4, 5), accepted=(5,))
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": b"edited after the claim"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "owner_evidence_tampered" and c["reason"] == "owner:hash_mismatch"
    assert c["ruled_mask"] == 0 and c["attempts"] == 0
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["stake_forfeited_wei"] == STAKE and loan["deducted_wei"] == CAPS[4]
    assert credit(contract, case.owner) == CAPS[4]
    assert credit(contract, case.holder) == DEPOSIT - CAPS[4] + STAKE
    check_books(contract, actors, [case])


def test_a_tampered_inventory_is_the_owners_fault_too(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.inv_url, {"status": 200, "body": b"a different inventory"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "owner_evidence_tampered" and c["reason"] == "owner:hash_mismatch"
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["stake_forfeited_wei"] == STAKE


def test_a_tampered_holder_report_loses_the_disputed_items_without_touching_the_stake(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": b"quietly rewritten"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "holder_evidence_tampered" and c["reason"] == "holder:hash_mismatch"
    assert c["ruled_mask"] == 0b01010
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["stake_forfeited_wei"] == 0
    assert loan["deducted_wei"] == CAPS[1] + CAPS[3]
    check_books(contract, actors, [case])


def test_the_owner_is_blamed_first_when_both_sides_served_wrong_bytes(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": b"owner swap"})
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": b"holder swap"})
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["ruling"] == "owner_evidence_tampered"


def test_an_unavailable_owner_document_is_judged_before_a_tampered_holder_document(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.owner_url, {"status": 503, "body": b"down"})
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": b"swapped"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "" and c["reason"] == "owner_unavailable"


# ---------------------------------------------------- other evidence faults
def _evidence_case(direct_vm, direct_deploy, actors, party, body):
    """A claim whose own committed bytes are `body`, hash included."""
    kwargs = {"owner_report": body} if party == "owner" else {"holder_report": body}
    return _setup(direct_vm, direct_deploy, actors, **kwargs)


@pytest.mark.parametrize("party", ["owner", "holder"])
@pytest.mark.parametrize("name,body,reason", [
    ("whitespace_only", b"  \n\t \n", "empty_artifact"),
    ("not_utf8", b"\xff\xfe\xfa binary", "invalid_utf8"),
    ("oversized", b"OVERSIZED " + b"x" * 30_000, "artifact_too_large"),
])
def test_committed_bytes_that_cannot_be_read_are_final_evidence_faults(
    direct_vm, direct_deploy, actors, party, name, body, reason
):
    contract, case = _evidence_case(direct_vm, direct_deploy, actors, party, body)
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    expected = "owner_evidence_tampered" if party == "owner" else "holder_evidence_tampered"
    assert c["ruling"] == expected and c["reason"] == f"{party}:{reason}"


@pytest.mark.parametrize("party", ["owner", "holder"])
def test_an_empty_body_is_a_final_evidence_fault(direct_vm, direct_deploy, actors, party):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    url = case.owner_url if party == "owner" else case.holder_url
    direct_vm.mock_web(url, {"status": 200, "body": b""})
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["reason"] == f"{party}:empty_artifact"


def test_a_report_at_exactly_the_size_limit_is_accepted(direct_vm, direct_deploy, actors):
    body = b"OWNERREPORT-EDGE: " + b"x" * (30_000 - len(b"OWNERREPORT-EDGE: "))
    assert len(body) == 30_000
    contract, case = _setup(direct_vm, direct_deploy, actors, name="edge", owner_report=body)
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["ruling"] == "ruled"


def test_a_non_2xx_status_is_never_an_evidence_fault_even_with_a_matching_body(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.owner_url, {"status": 404, "body": case.owner_report})
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["reason"] == "owner_unavailable"
    assert claim_of(contract, case)["ruling"] == ""


# --------------------------------------------------------- budget accounting
def test_every_failure_kind_consumes_the_call_budget_but_not_the_semantic_budget(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors)
    direct_vm.mock_web(case.holder_url, {"status": 500, "body": b"down"})
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["review_calls"] == 1 and c["attempts"] == 0
