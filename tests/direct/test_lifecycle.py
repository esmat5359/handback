"""Normal behavior of a loan, including exact deadline boundaries and role checks."""

import pytest

from tests.direct.support import (
    ACCEPT_BY, CAPS, CLAIM_WINDOW, DEPOSIT, ITEM_COUNT, NOW_TS, RESPOND_WINDOW, RETURN_BY,
    REVIEW_WINDOW, STAKE, Case, accept, addr, analysis, at, check_books, claim, claim_of,
    credit, deploy, deposit, deduction, digest, hand_back, loan_of, mask_of, open_loan, respond,
    review, serve, settle, to_review,
)


def _active(direct_vm, direct_deploy, actors, name="life"):
    contract = deploy(direct_vm, direct_deploy)
    case = Case(name, actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.warp(at(NOW_TS + 60))
    accept(contract, direct_vm, case)
    return contract, case


def test_info_reports_the_deployed_rules(direct_vm, direct_deploy):
    contract = deploy(direct_vm, direct_deploy)
    info = contract.get_info()
    assert info["name"] == "Handback"
    assert info["version"] == "0.1.0"
    assert info["max_items"] == 8
    assert info["minimum_confidence"] == 80
    assert info["max_review_calls_per_claim"] == 8
    assert info["max_semantic_assessments_per_claim"] == 3
    assert info["review_window_seconds"] == REVIEW_WINDOW
    assert "emits no transfers" in info["settlement_model"]


# ---------------------------------------------------------------- the offer
def test_accepting_locks_the_deposit_and_activates_the_loan(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    loan = loan_of(contract, case)
    assert loan["status"] == "active" and loan["accepted_at"] == NOW_TS + 60
    accounting = check_books(contract, actors, [case])
    assert accounting["total_locked_deposits"] == DEPOSIT
    assert accounting["total_locked_stakes"] == STAKE
    assert credit(contract, actors[1]) == 0


def test_only_the_named_holder_can_accept_and_only_with_the_committed_hash(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("acc", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    for who in (actors[0], actors[2]):
        deposit(contract, direct_vm, who, DEPOSIT)
        direct_vm.sender = who
        with direct_vm.expect_revert("Only the named holder may accept"):
            contract.accept_loan(addr(actors[0]), case.loan_id, digest(case.inventory))
    deposit(contract, direct_vm, actors[1], DEPOSIT)
    direct_vm.sender = actors[1]
    with direct_vm.expect_revert("does not match"):
        contract.accept_loan(addr(actors[0]), case.loan_id, digest(b"another inventory"))
    with direct_vm.expect_revert("Invalid SHA-256"):
        contract.accept_loan(addr(actors[0]), case.loan_id, "0x1234")
    assert loan_of(contract, case)["status"] == "offered"
    contract.accept_loan(addr(actors[0]), case.loan_id, digest(case.inventory).upper().replace("0X", "0x"))
    assert loan_of(contract, case)["status"] == "active"


def test_accepting_needs_the_full_deposit_in_credit(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("poor", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    deposit(contract, direct_vm, actors[1], DEPOSIT - 1)
    direct_vm.sender = actors[1]
    with direct_vm.expect_revert("Insufficient available credit"):
        contract.accept_loan(addr(actors[0]), case.loan_id, digest(case.inventory))
    assert credit(contract, actors[1]) == DEPOSIT - 1
    assert loan_of(contract, case)["status"] == "offered"


def test_acceptance_deadline_boundary_is_exact(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    late = Case("late", actors[0], actors[1])
    last = Case("last", actors[2], actors[3], loan_id="L-002")
    open_loan(contract, direct_vm, late)
    open_loan(contract, direct_vm, last)
    direct_vm.warp(at(ACCEPT_BY - 1))
    accept(contract, direct_vm, last)
    assert loan_of(contract, last)["status"] == "active"
    direct_vm.warp(at(ACCEPT_BY))
    deposit(contract, direct_vm, late.holder, DEPOSIT)
    direct_vm.sender = late.holder
    with direct_vm.expect_revert("Offer is not open"):
        contract.accept_loan(addr(late.owner), late.loan_id, digest(late.inventory))


def test_accepting_twice_or_after_cancellation_reverts(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    deposit(contract, direct_vm, case.holder, DEPOSIT)
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("Offer is not open"):
        contract.accept_loan(addr(case.owner), case.loan_id, digest(case.inventory))
    other = Case("canc", actors[2], actors[3], loan_id="L-009")
    open_loan(contract, direct_vm, other)
    direct_vm.sender = other.owner
    contract.cancel_offer(other.loan_id)
    deposit(contract, direct_vm, other.holder, DEPOSIT)
    direct_vm.sender = other.holder
    with direct_vm.expect_revert("Offer is not open"):
        contract.accept_loan(addr(other.owner), other.loan_id, digest(other.inventory))


def test_cancelling_an_unaccepted_offer_returns_the_stake(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("cancel", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.sender = actors[2]
    with direct_vm.expect_revert("Loan not found"):
        contract.cancel_offer(case.loan_id)
    direct_vm.sender = actors[1]
    with direct_vm.expect_revert("Loan not found"):
        contract.cancel_offer(case.loan_id)
    direct_vm.sender = actors[0]
    contract.cancel_offer(case.loan_id)
    loan = loan_of(contract, case)
    assert loan["status"] == "cancelled" and loan["settlement"] == "offer_cancelled"
    assert credit(contract, actors[0]) == STAKE
    check_books(contract, actors, [case])
    with direct_vm.expect_revert("Offer cannot be cancelled"):
        contract.cancel_offer(case.loan_id)
    assert credit(contract, actors[0]) == STAKE


def test_an_accepted_loan_cannot_be_cancelled(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("Offer cannot be cancelled"):
        contract.cancel_offer(case.loan_id)
    assert loan_of(contract, case)["status"] == "active"


def test_expiry_is_permissionless_but_only_after_the_deadline(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("exp", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.sender = actors[5]
    direct_vm.warp(at(ACCEPT_BY - 1))
    with direct_vm.expect_revert("Offer has not expired"):
        contract.expire_offer(addr(case.owner), case.loan_id)
    direct_vm.warp(at(ACCEPT_BY))
    contract.expire_offer(addr(case.owner), case.loan_id)
    loan = loan_of(contract, case)
    assert loan["status"] == "cancelled" and loan["settlement"] == "offer_expired"
    assert credit(contract, case.owner) == STAKE
    check_books(contract, actors, [case])
    with direct_vm.expect_revert("Offer has not expired"):
        contract.expire_offer(addr(case.owner), case.loan_id)


def test_an_accepted_loan_cannot_expire(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(ACCEPT_BY + 10))
    direct_vm.sender = actors[5]
    with direct_vm.expect_revert("Offer has not expired"):
        contract.expire_offer(addr(case.owner), case.loan_id)


# ------------------------------------------------------------------ hand back
def test_hand_back_records_the_return_and_an_optional_report(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(NOW_TS + 120))
    hand_back(contract, direct_vm, case)
    loan = loan_of(contract, case)
    assert loan["returned_at"] == NOW_TS + 120
    assert loan["holder_report_url"] == case.holder_url
    assert loan["holder_report_hash"] == case.holder_hash
    # The claim deadline is anchored to the due date for an early return.
    assert loan["claim_by"] == RETURN_BY + CLAIM_WINDOW


def test_hand_back_without_a_report_is_allowed(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    hand_back(contract, direct_vm, case, with_report=False)
    loan = loan_of(contract, case)
    assert loan["returned_at"] > 0 and loan["holder_report_url"] == ""


@pytest.mark.parametrize("url,digest_text,message", [
    ("https://loans.example.org/r.md", "", "both a URL and a SHA-256"),
    ("", "0x" + "ab" * 32, "both a URL and a SHA-256"),
    ("http://loans.example.org/r.md", "0x" + "ab" * 32, "Invalid HTTPS URL"),
    ("https://loans.example.org/r.md", "0x12", "Invalid SHA-256"),
])
def test_malformed_hand_back_reports_revert(direct_vm, direct_deploy, actors, url, digest_text, message):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.holder
    with direct_vm.expect_revert(message):
        contract.hand_back(addr(case.owner), case.loan_id, url, digest_text)
    assert loan_of(contract, case)["returned_at"] == 0


def test_only_the_holder_can_hand_back_and_only_once_while_active(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    for who in (case.owner, actors[5]):
        direct_vm.sender = who
        with direct_vm.expect_revert("Only the holder may hand back"):
            contract.hand_back(addr(case.owner), case.loan_id, "", "")
    hand_back(contract, direct_vm, case)
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("cannot be handed back"):
        contract.hand_back(addr(case.owner), case.loan_id, "", "")


def test_hand_back_before_acceptance_reverts(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("early", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("cannot be handed back"):
        contract.hand_back(addr(case.owner), case.loan_id, "", "")


def test_hand_back_window_closes_when_the_claim_window_would_have_ended(
    direct_vm, direct_deploy, actors
):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW))
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("Return window is over"):
        contract.hand_back(addr(case.owner), case.loan_id, "", "")
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW - 1))
    contract.hand_back(addr(case.owner), case.loan_id, "", "")
    assert loan_of(contract, case)["returned_at"] == RETURN_BY + CLAIM_WINDOW - 1


def test_a_late_hand_back_extends_the_owners_claim_window(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    late = RETURN_BY + CLAIM_WINDOW - 5
    direct_vm.warp(at(late))
    hand_back(contract, direct_vm, case)
    assert loan_of(contract, case)["claim_by"] == late + CLAIM_WINDOW
    direct_vm.warp(at(late + CLAIM_WINDOW - 1))
    claim(contract, direct_vm, case, [1])
    assert loan_of(contract, case)["status"] == "claimed"


# ------------------------------------------------------------------ claiming
def test_claim_is_blocked_until_the_property_is_due_or_returned(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY - 1))
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("not due back yet"):
        contract.file_claim(case.loan_id, 1, case.owner_url, case.owner_hash)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, [1])
    assert loan_of(contract, case)["status"] == "claimed"


def test_a_returned_loan_can_be_claimed_immediately(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(NOW_TS + 120))
    hand_back(contract, direct_vm, case)
    claim(contract, direct_vm, case, [2, 3])
    c = claim_of(contract, case)
    assert c["claimed_items"] == [2, 3] and c["claimed_mask"] == 0b110
    assert c["respond_by"] == NOW_TS + 120 + RESPOND_WINDOW
    assert c["owner_report_url"] == case.owner_url


def test_claim_deadline_boundary_is_exact(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW))
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("Claim window is over"):
        contract.file_claim(case.loan_id, 1, case.owner_url, case.owner_hash)
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW - 1))
    claim(contract, direct_vm, case, [1])
    assert loan_of(contract, case)["status"] == "claimed"


@pytest.mark.parametrize("mask,message", [
    (0, "Invalid claim mask"),
    (1 << ITEM_COUNT, "Invalid claim mask"),
    ((1 << ITEM_COUNT) | 1, "Invalid claim mask"),
    (0xFFFF, "Invalid claim mask"),
])
def test_claim_masks_must_name_existing_items(direct_vm, direct_deploy, actors, mask, message):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY))
    direct_vm.sender = case.owner
    with direct_vm.expect_revert(message):
        contract.file_claim(case.loan_id, mask, case.owner_url, case.owner_hash)
    assert loan_of(contract, case)["status"] == "active"


@pytest.mark.parametrize("url,digest_text,message", [
    ("", "", "Invalid HTTPS URL"),
    ("https://loans.example.org/r.md", "", "Invalid SHA-256"),
    ("http://loans.example.org/r.md", "0x" + "ab" * 32, "Invalid HTTPS URL"),
    ("https://loans.example.org/r.md", "0x12", "Invalid SHA-256"),
])
def test_a_claim_must_carry_a_valid_report(direct_vm, direct_deploy, actors, url, digest_text, message):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY))
    direct_vm.sender = case.owner
    with direct_vm.expect_revert(message):
        contract.file_claim(case.loan_id, 1, url, digest_text)
    assert loan_of(contract, case)["status"] == "active"


def test_only_the_owner_can_claim_and_only_once(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY))
    for who in (case.holder, actors[5]):
        direct_vm.sender = who
        with direct_vm.expect_revert("Loan not found"):
            contract.file_claim(case.loan_id, 1, case.owner_url, case.owner_hash)
    claim(contract, direct_vm, case, [1])
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("Loan is not active"):
        contract.file_claim(case.loan_id, 2, case.owner_url, case.owner_hash)
    assert claim_of(contract, case)["claimed_mask"] == 1


def test_claiming_an_unaccepted_offer_reverts(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("noacc", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY))
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("Loan is not active"):
        contract.file_claim(case.loan_id, 1, case.owner_url, case.owner_hash)


def test_hand_back_after_a_claim_reverts(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, [1])
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("cannot be handed back"):
        contract.hand_back(addr(case.owner), case.loan_id, case.holder_url, case.holder_hash)


# ------------------------------------------------------------- no-claim exit
def test_a_loan_nobody_claims_returns_the_whole_deposit(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    hand_back(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW - 1))
    with direct_vm.expect_revert("Claim window is still open"):
        settle(contract, direct_vm, actors[7], case)
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW))
    settle(contract, direct_vm, actors[7], case)  # permissionless
    loan = loan_of(contract, case)
    assert loan["status"] == "settled" and loan["settlement"] == "no_claim_deposit_returned"
    assert loan["deducted_wei"] == 0 and loan["returned_wei"] == DEPOSIT
    assert credit(contract, case.holder) == DEPOSIT
    assert credit(contract, case.owner) == STAKE
    check_books(contract, actors, [case])
    with direct_vm.expect_revert("cannot be settled"):
        settle(contract, direct_vm, actors[7], case)
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("Loan is not active"):
        contract.file_claim(case.loan_id, 1, case.owner_url, case.owner_hash)


def test_the_claim_deadline_for_an_unreturned_loan_is_anchored_to_the_due_date(
    direct_vm, direct_deploy, actors
):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY + CLAIM_WINDOW))
    settle(contract, direct_vm, actors[7], case)
    assert loan_of(contract, case)["settlement"] == "no_claim_deposit_returned"


# ------------------------------------------------------------------ responding
def _claimed(direct_vm, direct_deploy, actors, items=(1, 2, 3), name="resp"):
    contract = deploy(direct_vm, direct_deploy)
    case = Case(name, actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.warp(at(NOW_TS + 60))
    accept(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, list(items))
    return contract, case


def test_accepting_every_claimed_item_settles_immediately_without_validators(
    direct_vm, direct_deploy, actors
):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    respond(contract, direct_vm, case, [1, 2, 3])
    loan = loan_of(contract, case)
    assert loan["status"] == "settled" and loan["settlement"] == "claim_accepted_by_holder"
    expected = CAPS[0] + CAPS[1] + CAPS[2]
    assert loan["deducted_wei"] == expected and loan["returned_wei"] == DEPOSIT - expected
    assert loan["deducted_mask"] == 0b111
    assert credit(contract, case.owner) == expected + STAKE
    assert credit(contract, case.holder) == DEPOSIT - expected
    check_books(contract, actors, [case])


def test_partial_acceptance_leaves_the_rest_disputed(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    respond(contract, direct_vm, case, [2])
    c = claim_of(contract, case)
    assert c["accepted_mask"] == 0b010 and c["disputed_items"] == [1, 3]
    assert loan_of(contract, case)["status"] == "claimed"
    assert c["review_start"] == c["responded_at"]
    check_books(contract, actors, [case])


def test_rejecting_everything_disputes_every_claimed_item(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    respond(contract, direct_vm, case, [])
    assert claim_of(contract, case)["disputed_items"] == [1, 2, 3]


def test_accepted_items_must_be_part_of_the_claim(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.holder
    for mask in (0b1000, 0b1001, 1 << 15):
        with direct_vm.expect_revert("part of the claim"):
            contract.respond_to_claim(addr(case.owner), case.loan_id, mask, "", "")
    assert claim_of(contract, case)["responded_at"] == 0


def test_only_the_holder_can_respond_and_only_once(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    for who in (case.owner, actors[5]):
        direct_vm.sender = who
        with direct_vm.expect_revert("Only the holder may respond"):
            contract.respond_to_claim(addr(case.owner), case.loan_id, 0, "", "")
    respond(contract, direct_vm, case, [1])
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("No claim awaiting a response"):
        contract.respond_to_claim(addr(case.owner), case.loan_id, 0b10, "", "")
    assert claim_of(contract, case)["accepted_mask"] == 1


def test_response_deadline_boundary_is_exact(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    late = Case("rlate", actors[0], actors[1])
    last = Case("rlast", actors[2], actors[3], loan_id="L-002")
    for case in (late, last):
        open_loan(contract, direct_vm, case)
    direct_vm.warp(at(NOW_TS + 60))
    for case in (late, last):
        accept(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY))
    for case in (late, last):
        claim(contract, direct_vm, case, [1, 2])
    direct_vm.warp(at(RETURN_BY + RESPOND_WINDOW - 1))
    respond(contract, direct_vm, last, [1])
    assert claim_of(contract, last)["accepted_mask"] == 1
    direct_vm.warp(at(RETURN_BY + RESPOND_WINDOW))
    direct_vm.sender = late.holder
    with direct_vm.expect_revert("Response window is over"):
        contract.respond_to_claim(addr(late.owner), late.loan_id, 1, "", "")


def test_the_holder_may_attach_a_report_with_the_response_when_none_exists(
    direct_vm, direct_deploy, actors
):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    respond(contract, direct_vm, case, [1], with_report=True)
    loan = loan_of(contract, case)
    assert loan["holder_report_url"] == case.holder_url
    assert loan["holder_report_hash"] == case.holder_hash


def test_a_second_holder_report_cannot_replace_the_first(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("twice", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    direct_vm.warp(at(NOW_TS + 60))
    accept(contract, direct_vm, case)
    hand_back(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, [1, 2])
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("Holder report already committed"):
        contract.respond_to_claim(addr(case.owner), case.loan_id, 1,
                                  "https://loans.example.org/other.md", digest(b"other"))
    assert loan_of(contract, case)["holder_report_url"] == case.holder_url
    assert claim_of(contract, case)["responded_at"] == 0
    respond(contract, direct_vm, case, [1])  # the plain response still works


@pytest.mark.parametrize("url,digest_text,message", [
    ("https://loans.example.org/r.md", "", "both a URL and a SHA-256"),
    ("", "0x" + "ab" * 32, "both a URL and a SHA-256"),
    ("ftp://loans.example.org/r.md", "0x" + "ab" * 32, "Invalid HTTPS URL"),
])
def test_malformed_response_reports_revert(direct_vm, direct_deploy, actors, url, digest_text, message):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.holder
    with direct_vm.expect_revert(message):
        contract.respond_to_claim(addr(case.owner), case.loan_id, 1, url, digest_text)
    assert claim_of(contract, case)["responded_at"] == 0


# ------------------------------------------------------------ withdraw claim
def test_the_owner_can_withdraw_a_claim_and_accepted_items_still_stand(
    direct_vm, direct_deploy, actors
):
    contract, case = _claimed(direct_vm, direct_deploy, actors, items=(1, 2, 3))
    respond(contract, direct_vm, case, [3])
    direct_vm.sender = case.owner
    contract.withdraw_claim(case.loan_id)
    loan = loan_of(contract, case)
    assert loan["status"] == "settled" and loan["settlement"] == "review_claim_withdrawn"
    assert loan["deducted_wei"] == CAPS[2] and loan["deducted_mask"] == 0b100
    assert credit(contract, case.holder) == DEPOSIT - CAPS[2]
    assert credit(contract, case.owner) == CAPS[2] + STAKE
    check_books(contract, actors, [case])


def test_withdrawing_before_any_response_releases_every_item(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.owner
    contract.withdraw_claim(case.loan_id)
    assert loan_of(contract, case)["deducted_wei"] == 0
    assert credit(contract, case.holder) == DEPOSIT
    # The holder can no longer respond to a settled claim.
    direct_vm.sender = case.holder
    with direct_vm.expect_revert("No claim awaiting a response"):
        contract.respond_to_claim(addr(case.owner), case.loan_id, 0, "", "")


def test_only_the_owner_can_withdraw_and_only_an_open_claim(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    for who in (case.holder, actors[5]):
        direct_vm.sender = who
        with direct_vm.expect_revert("Loan not found"):
            contract.withdraw_claim(case.loan_id)
    direct_vm.sender = case.owner
    contract.withdraw_claim(case.loan_id)
    with direct_vm.expect_revert("No open claim to withdraw"):
        contract.withdraw_claim(case.loan_id)


def test_withdrawing_without_a_claim_reverts(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("No open claim to withdraw"):
        contract.withdraw_claim(case.loan_id)


# ------------------------------------------------------------------- silence
def test_a_holder_who_never_responds_is_reviewed_on_the_owners_evidence_alone(
    direct_vm, direct_deploy, actors
):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    case.verdict = analysis(["damaged", "not_damaged", "damaged"])
    serve(direct_vm, [case], with_holder=False)
    direct_vm.warp(at(RETURN_BY + RESPOND_WINDOW - 1))
    with direct_vm.expect_revert("Review window is not open"):
        review(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY + RESPOND_WINDOW))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "ruled" and c["ruled_mask"] == 0b101
    settle(contract, direct_vm, actors[8], case)
    assert loan_of(contract, case)["deducted_wei"] == CAPS[0] + CAPS[2]
    check_books(contract, actors, [case])


def test_unreviewed_silence_defaults_to_no_deduction_only_after_the_review_window(
    direct_vm, direct_deploy, actors
):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    direct_vm.warp(at(RETURN_BY + RESPOND_WINDOW + REVIEW_WINDOW - 1))
    with direct_vm.expect_revert("still under review"):
        settle(contract, direct_vm, actors[8], case)
    direct_vm.warp(at(RETURN_BY + RESPOND_WINDOW + REVIEW_WINDOW))
    settle(contract, direct_vm, actors[8], case)
    loan = loan_of(contract, case)
    assert loan["settlement"] == "review_burden_unmet" and loan["deducted_wei"] == 0
    assert credit(contract, case.holder) == DEPOSIT
    assert credit(contract, case.owner) == STAKE
    check_books(contract, actors, [case])


def test_settling_during_the_response_window_reverts(direct_vm, direct_deploy, actors):
    contract, case = _claimed(direct_vm, direct_deploy, actors)
    with direct_vm.expect_revert("still under review"):
        settle(contract, direct_vm, actors[8], case)


def test_an_offered_or_settled_loan_cannot_be_settled(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("off", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    with direct_vm.expect_revert("cannot be settled"):
        settle(contract, direct_vm, actors[8], case)


# ------------------------------------------------------------------- credits
def test_credit_withdrawal_is_caller_owned_and_cleared_first(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    deposit(contract, direct_vm, actors[0], 5 * 10**14)
    direct_vm.sender = actors[1]
    with direct_vm.expect_revert("No available credit"):
        contract.withdraw_credit()
    direct_vm.sender = actors[0]
    contract.withdraw_credit()
    assert credit(contract, actors[0]) == 0
    assert contract.get_accounting()["total_credits"] == 0
    with direct_vm.expect_revert("No available credit"):
        contract.withdraw_credit()


def test_zero_value_deposits_revert(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    direct_vm.sender = actors[0]
    direct_vm.value = 0
    with direct_vm.expect_revert("Deposit must be positive"):
        contract.deposit_credit()


# -------------------------------------------------------------------- release
def test_the_owner_can_release_a_returned_loan_early(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    hand_back(contract, direct_vm, case)
    direct_vm.sender = case.owner
    contract.release_loan(case.loan_id)
    loan = loan_of(contract, case)
    assert loan["status"] == "settled" and loan["settlement"] == "released_by_owner"
    assert loan["deducted_wei"] == 0 and credit(contract, case.holder) == DEPOSIT
    assert credit(contract, case.owner) == STAKE
    check_books(contract, actors, [case])


def test_release_is_blocked_before_the_property_is_back_or_due(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("not due back yet"):
        contract.release_loan(case.loan_id)
    direct_vm.warp(at(RETURN_BY))
    contract.release_loan(case.loan_id)  # past the due date the owner may release too
    assert loan_of(contract, case)["status"] == "settled"


def test_only_the_owner_can_release_and_only_an_active_loan(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    hand_back(contract, direct_vm, case)
    for who in (case.holder, actors[5]):
        direct_vm.sender = who
        with direct_vm.expect_revert("Loan not found"):
            contract.release_loan(case.loan_id)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, [1])
    direct_vm.sender = case.owner
    with direct_vm.expect_revert("Loan is not active"):
        contract.release_loan(case.loan_id)
    other = Case("rel2", actors[2], actors[3], loan_id="L-REL")
    open_loan(contract, direct_vm, other, accept_by=RETURN_BY + 7200, return_by=RETURN_BY + 14400)
    direct_vm.sender = other.owner
    with direct_vm.expect_revert("Loan is not active"):
        contract.release_loan(other.loan_id)  # still only an offer


def test_a_released_loan_cannot_be_claimed_or_released_again(direct_vm, direct_deploy, actors):
    contract, case = _active(direct_vm, direct_deploy, actors)
    hand_back(contract, direct_vm, case)
    direct_vm.sender = case.owner
    contract.release_loan(case.loan_id)
    with direct_vm.expect_revert("Loan is not active"):
        contract.release_loan(case.loan_id)
    with direct_vm.expect_revert("Loan is not active"):
        contract.file_claim(case.loan_id, 1, case.owner_url, case.owner_hash)


# ---------------------------------------------------------------- large values
def test_amounts_far_above_64_bits_round_trip_through_storage_and_views(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("big", actors[0], actors[1], verdict=analysis(["damaged", "not_damaged"]))
    big_caps = [40 * 10**18, 30 * 10**18, 25 * 10**18, 20 * 10**18, 15 * 10**18]
    big_deposit, big_stake = 200 * 10**18, 5 * 10**18
    open_loan(contract, direct_vm, case, caps=big_caps, deposit_wei=big_deposit, stake=big_stake)
    direct_vm.warp(at(NOW_TS + 60))
    accept(contract, direct_vm, case, deposit_wei=big_deposit)
    hand_back(contract, direct_vm, case)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, [2, 4, 5])
    respond(contract, direct_vm, case, [5])
    serve(direct_vm, [case])
    review(contract, direct_vm, case)
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["caps"] == big_caps and loan["deposit_wei"] == big_deposit
    assert loan["deducted_wei"] == big_caps[1] + big_caps[4]
    assert credit(contract, case.owner) == big_caps[1] + big_caps[4] + big_stake
    assert credit(contract, case.holder) == big_deposit - big_caps[1] - big_caps[4]
    assert check_books(contract, actors, [case])["total_credits"] == big_deposit + big_stake


def test_many_loans_are_enumerable_through_the_index(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    owners = [actors[0], actors[2]]
    refs = []
    for index in range(24):
        case = Case(f"n{index}", owners[index % 2], actors[1], loan_id=f"N{index}")
        open_loan(contract, direct_vm, case)
        refs.append((addr(case.owner), case.loan_id))
    assert contract.get_loan_count() == 24
    listed = [contract.get_loan_ref(i) for i in range(24)]
    assert [(r["owner"], r["loan_id"]) for r in listed] == refs
    for owner_hex, loan_id in refs:
        assert contract.get_loan(owner_hex, loan_id)["status"] == "offered"
