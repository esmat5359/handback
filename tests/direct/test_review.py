"""Validator review: classification, fault attribution, budgets and fallbacks."""

import json

import pytest

from tests.direct.support import (
    CAPS, DEPOSIT, NOW_TS, PROMPT_HEAD, RESPOND_WINDOW, RETURN_BY, REVIEW_WINDOW, STAKE, Case,
    analysis, at, check_books, claim_of, credit, deploy, loan_of, review, serve, settle,
    to_review,
)

REVIEW_AT = RETURN_BY + 60


def _setup(direct_vm, direct_deploy, actors, verdict, claimed=(2, 4), accepted=(), name="rv",
           holder_report=True, **case_kwargs):
    contract = deploy(direct_vm, direct_deploy)
    case = Case(name, actors[0], actors[1], verdict=verdict, **case_kwargs)
    to_review(contract, direct_vm, case, list(claimed), list(accepted), holder_report=holder_report)
    return contract, case


def _ruled(contract, case):
    return claim_of(contract, case)["ruling"]


# ------------------------------------------------------------------ rulings
def test_damaged_and_not_damaged_items_are_ruled_individually(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "not_damaged"]))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "ruled" and c["ruled_mask"] == 0b00010
    assert c["confidence"] == 92 and c["owner_instruction_attempt"] == "no"
    assert c["attempts"] == 1 and c["review_calls"] == 1 and c["reason"] == ""
    assert loan_of(contract, case)["status"] == "claimed"  # settlement is a separate step
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["deducted_mask"] == 0b00010 and loan["deducted_wei"] == CAPS[1]
    assert loan["settlement"] == "review_ruled"
    assert credit(contract, case.owner) == CAPS[1] + STAKE
    assert credit(contract, case.holder) == DEPOSIT - CAPS[1]
    check_books(contract, actors, [case])


def test_every_disputed_item_cleared_deducts_nothing(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["not_damaged", "not_damaged"]))
    review(contract, direct_vm, case)
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["deducted_wei"] == 0
    assert credit(contract, case.holder) == DEPOSIT
    assert credit(contract, case.owner) == STAKE


def test_every_disputed_item_damaged_deducts_each_cap(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"]))
    review(contract, direct_vm, case)
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["deducted_wei"] == CAPS[1] + CAPS[3]
    assert credit(contract, case.holder) == DEPOSIT - CAPS[1] - CAPS[3]


def test_verdict_positions_map_to_the_right_non_contiguous_items(direct_vm, direct_deploy, actors):
    contract, case = _setup(
        direct_vm, direct_deploy, actors, analysis(["not_damaged", "damaged", "damaged"]),
        claimed=(1, 3, 5),
    )
    assert claim_of(contract, case)["disputed_items"] == [1, 3, 5]
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["ruled_mask"] == 0b10100
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["deducted_wei"] == CAPS[2] + CAPS[4]


def test_accepted_items_are_deducted_even_when_the_rest_is_cleared(direct_vm, direct_deploy, actors):
    contract, case = _setup(
        direct_vm, direct_deploy, actors, analysis(["not_damaged"]), claimed=(2, 4, 5), accepted=(2, 5),
    )
    assert claim_of(contract, case)["disputed_items"] == [4]
    review(contract, direct_vm, case)
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["deducted_mask"] == 0b10010
    assert loan["deducted_wei"] == CAPS[1] + CAPS[4]


def test_only_the_disputed_items_are_sent_to_the_model(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("only", actors[0], actors[1])  # no default verdict mock
    to_review(contract, direct_vm, case, [1, 3, 4], [1])
    direct_vm.mock_llm(
        PROMPT_HEAD + r".*in this order: I3, I4\. .*exactly 2 strings",
        json.dumps(analysis(["damaged", "not_damaged"])),
    )
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["ruled_mask"] == 0b00100


def test_the_prompt_never_mentions_money(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("money", actors[0], actors[1])
    to_review(contract, direct_vm, case, [1, 2])
    amounts = "|".join(str(cap) for cap in CAPS + [DEPOSIT, STAKE])
    direct_vm.mock_llm(r"(?s)\A(?!.*(?:" + amounts + r")).*You are independently reviewing",
                       json.dumps(analysis(["damaged", "damaged"])))
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "ruled"


def test_the_prompt_says_when_there_is_no_holder_report(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("nohr", actors[0], actors[1])
    to_review(contract, direct_vm, case, [1, 2], holder_report=False)
    direct_vm.mock_llm(
        PROMPT_HEAD + r".*BEGIN_UNTRUSTED_HOLDER_REPORT\n\(the holder submitted no report\)\nEND",
        json.dumps(analysis(["damaged", "damaged"])),
    )
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "ruled"


def test_the_prompt_carries_all_three_documents(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("all3", actors[0], actors[1])
    to_review(contract, direct_vm, case, [1, 2])
    direct_vm.mock_llm(
        PROMPT_HEAD + r".*BEGIN_UNTRUSTED_INVENTORY\n# Handover inventory.*"
        r"BEGIN_UNTRUSTED_OWNER_REPORT\nOWNERREPORT-ALL3.*"
        r"BEGIN_UNTRUSTED_HOLDER_REPORT\nHOLDERREPORT-ALL3",
        json.dumps(analysis(["damaged", "damaged"])),
    )
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "ruled"


def test_a_holder_report_added_with_the_response_reaches_the_model(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("late", actors[0], actors[1])
    to_review(contract, direct_vm, case, [1, 2], with_hand_back=False, holder_report=False,
              respond_with_report=True)
    direct_vm.mock_llm(
        PROMPT_HEAD + r".*BEGIN_UNTRUSTED_HOLDER_REPORT\nHOLDERREPORT-LATE",
        json.dumps(analysis(["not_damaged", "damaged"])),
    )
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["ruled_mask"] == 0b10


# --------------------------------------------------------- final and gated
def test_a_decisive_verdict_is_final(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "not_damaged"]))
    review(contract, direct_vm, case)
    direct_vm.clear_mocks()
    case.verdict = analysis(["not_damaged", "damaged"])
    serve(direct_vm, [case])
    direct_vm.warp(at(REVIEW_AT + 7200))
    with direct_vm.expect_revert("Claim already ruled"):
        review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruled_mask"] == 0b00010 and c["review_calls"] == 1


def test_only_the_parties_may_request_a_review_and_both_can(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"]))
    for stranger in (actors[5], actors[9]):
        with direct_vm.expect_revert("Only the owner or the holder"):
            review(contract, direct_vm, case, caller=stranger)
    assert claim_of(contract, case)["review_calls"] == 0
    review(contract, direct_vm, case, caller=case.holder)
    assert _ruled(contract, case) == "ruled"


def test_review_needs_an_open_claim_with_something_disputed(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("none", actors[0], actors[1], verdict=analysis(["damaged"]))
    to_review(contract, direct_vm, case, [1], [1])  # everything accepted: already settled
    assert loan_of(contract, case)["status"] == "settled"
    with direct_vm.expect_revert("No claim to review"):
        review(contract, direct_vm, case)


def test_review_before_a_claim_reverts(direct_vm, direct_deploy, actors):
    from tests.direct.support import accept, open_loan
    contract = deploy(direct_vm, direct_deploy)
    case = Case("pre", actors[0], actors[1], verdict=analysis(["damaged"]))
    open_loan(contract, direct_vm, case)
    with direct_vm.expect_revert("No claim to review"):
        review(contract, direct_vm, case)
    direct_vm.warp(at(NOW_TS + 60))
    accept(contract, direct_vm, case)
    with direct_vm.expect_revert("No claim to review"):
        review(contract, direct_vm, case)


def test_review_window_boundaries(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"]))
    responded = claim_of(contract, case)["responded_at"]
    direct_vm.warp(at(responded + REVIEW_WINDOW))
    with direct_vm.expect_revert("Review window is not open"):
        review(contract, direct_vm, case)
    # Settlement is available once the window is over: burden of proof stays with the owner.
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["settlement"] == "review_burden_unmet"


def test_the_last_second_of_the_review_window_still_works(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"]))
    responded = claim_of(contract, case)["responded_at"]
    direct_vm.warp(at(responded + REVIEW_WINDOW - 1))
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "ruled"


# ------------------------------------------------------------ inconclusive
@pytest.mark.parametrize("name,verdict", [
    ("unclear_item", analysis(["damaged", "unclear"])),
    ("all_unclear", analysis(["unclear", "unclear"])),
    ("low_confidence", analysis(["damaged", "damaged"], confidence=79)),
    ("unclear_owner_flag", analysis(["damaged", "damaged"], owner="unclear")),
    ("unclear_holder_flag", analysis(["damaged", "damaged"], holder="unclear")),
    ("low_confidence_foul", analysis(["damaged", "damaged"], owner="yes", confidence=79)),
])
def test_inconclusive_output_decides_nothing_and_enforces_a_cooldown(
    direct_vm, direct_deploy, actors, name, verdict
):
    contract, case = _setup(direct_vm, direct_deploy, actors, verdict)
    direct_vm.warp(at(REVIEW_AT))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "" and c["ruled_mask"] == 0
    assert c["attempts"] == 1 and c["review_calls"] == 1
    assert c["retry_at"] == REVIEW_AT + 60
    with direct_vm.expect_revert("Review cooldown is active"):
        review(contract, direct_vm, case)
    direct_vm.warp(at(REVIEW_AT + 59))
    with direct_vm.expect_revert("Review cooldown is active"):
        review(contract, direct_vm, case)
    assert loan_of(contract, case)["status"] == "claimed"


def test_a_decisive_verdict_can_follow_an_inconclusive_one(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["unclear", "unclear"]))
    direct_vm.warp(at(REVIEW_AT))
    review(contract, direct_vm, case)
    direct_vm.clear_mocks()
    case.verdict = analysis(["damaged", "not_damaged"])
    serve(direct_vm, [case])
    direct_vm.warp(at(REVIEW_AT + 60))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "ruled" and c["ruled_mask"] == 0b10 and c["attempts"] == 2


def test_confidence_threshold_is_inclusive_at_eighty(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"], confidence=80))
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "ruled"


def test_three_inconclusive_assessments_exhaust_the_semantic_budget(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["unclear", "unclear"]))
    moment = REVIEW_AT
    for _ in range(3):
        direct_vm.warp(at(moment))
        review(contract, direct_vm, case)
        moment = claim_of(contract, case)["retry_at"]
    direct_vm.warp(at(moment))
    with direct_vm.expect_revert("Review budget exhausted"):
        review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["attempts"] == 3 and c["review_calls"] == 3
    # Exhaustion lets anyone settle right away, with no deduction for the disputed items.
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["settlement"] == "review_burden_unmet" and loan["deducted_wei"] == 0
    assert credit(contract, case.holder) == DEPOSIT and credit(contract, case.owner) == STAKE


def test_owner_side_outages_double_the_cooldown_until_the_fourth_strike_rules(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"]))
    direct_vm.mock_web(case.owner_url, {"status": 503, "body": b"down"})
    moment = REVIEW_AT
    delays = []
    for _ in range(4):
        direct_vm.warp(at(moment))
        review(contract, direct_vm, case)
        retry_at = claim_of(contract, case)["retry_at"]
        delays.append(retry_at - moment)
        moment = retry_at
    assert delays == [60, 120, 240, 480]
    assert _ruled(contract, case) == "owner_evidence_unavailable"
    direct_vm.warp(at(moment))
    with direct_vm.expect_revert("Claim already ruled"):
        review(contract, direct_vm, case)


def test_the_call_ladder_without_any_ruling_runs_to_eight(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"]))
    direct_vm.clear_mocks()
    direct_vm.mock_web(case.inv_url, {"status": 200, "body": case.inventory})
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": case.owner_report})
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": case.holder_report})
    # No model mock at all: every call is an observation failure that charges the budget.
    moment = REVIEW_AT
    delays = []
    for _ in range(8):
        direct_vm.warp(at(moment))
        review(contract, direct_vm, case)
        retry_at = claim_of(contract, case)["retry_at"]
        delays.append(retry_at - moment)
        moment = retry_at
    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    direct_vm.warp(at(moment))
    with direct_vm.expect_revert("Review budget exhausted"):
        review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["review_calls"] == 8 and c["attempts"] == 0
    assert c["reason"] == "observation_failure" and c["ruling"] == ""
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["settlement"] == "review_burden_unmet"


# ------------------------------------------------------------- foul rulings
def test_an_owner_instruction_attempt_voids_the_disputed_items_but_not_the_stake(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors,
                            analysis(["damaged", "damaged"], owner="yes"),
                            claimed=(2, 4, 5), accepted=(2,))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "owner_foul" and c["ruled_mask"] == 0
    assert c["owner_instruction_attempt"] == "yes"
    settle(contract, direct_vm, actors[9], case)
    loan = loan_of(contract, case)
    assert loan["settlement"] == "review_owner_foul"
    assert loan["deducted_mask"] == 0b00010 and loan["stake_forfeited_wei"] == 0
    assert credit(contract, case.owner) == CAPS[1] + STAKE
    check_books(contract, actors, [case])


def test_a_holder_instruction_attempt_loses_every_disputed_item(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors,
                            analysis(["not_damaged", "not_damaged"], holder="yes"))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "holder_foul" and c["ruled_mask"] == 0b01010
    settle(contract, direct_vm, actors[9], case)
    assert loan_of(contract, case)["deducted_wei"] == CAPS[1] + CAPS[3]
    assert credit(contract, case.holder) == DEPOSIT - CAPS[1] - CAPS[3]


def test_when_both_sides_try_to_steer_the_reviewer_the_owner_carries_the_burden(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors,
                            analysis(["damaged", "damaged"], owner="yes", holder="yes"))
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "owner_foul"


def test_a_foul_ruling_does_not_need_clear_item_labels(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors,
                            analysis(["unclear", "unclear"], owner="yes", confidence=80))
    review(contract, direct_vm, case)
    assert _ruled(contract, case) == "owner_foul"


def test_model_labels_are_case_and_whitespace_normalised(direct_vm, direct_deploy, actors):
    verdict = {
        "items": [" DAMAGED ", "Not_Damaged"], "owner_instruction_attempt": " No ",
        "holder_instruction_attempt": "NO", "confidence": 90, "rationale": "  fine\n\n ok ",
    }
    contract, case = _setup(direct_vm, direct_deploy, actors, verdict)
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruled_mask"] == 0b10 and c["rationale"] == "fine ok"


def test_overlong_rationale_is_truncated_not_rejected(direct_vm, direct_deploy, actors):
    contract, case = _setup(direct_vm, direct_deploy, actors,
                            analysis(["damaged", "damaged"], rationale="r" * 900))
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "ruled" and len(c["rationale"]) == 300


@pytest.mark.parametrize("name,payload", [
    ("missing_key", {k: v for k, v in analysis(["damaged", "damaged"]).items() if k != "rationale"}),
    ("extra_key", {**analysis(["damaged", "damaged"]), "approve": True}),
    ("short_list", analysis(["damaged"])),
    ("long_list", analysis(["damaged", "damaged", "damaged"])),
    ("bad_item", analysis(["damaged", "broken"])),
    ("nonstring_item", {**analysis(["damaged", "damaged"]), "items": ["damaged", 1]}),
    ("list_is_string", {**analysis(["damaged", "damaged"]), "items": "damaged,damaged"}),
    ("bad_owner_flag", analysis(["damaged", "damaged"], owner="maybe")),
    ("bad_holder_flag", analysis(["damaged", "damaged"], holder=True)),
    ("bool_confidence", {**analysis(["damaged", "damaged"]), "confidence": True}),
    ("float_confidence", {**analysis(["damaged", "damaged"]), "confidence": 90.5}),
    ("string_confidence", {**analysis(["damaged", "damaged"]), "confidence": "95"}),
    ("confidence_over_100", analysis(["damaged", "damaged"], confidence=101)),
    ("negative_confidence", analysis(["damaged", "damaged"], confidence=-1)),
    ("rationale_not_text", {**analysis(["damaged", "damaged"]), "rationale": 7}),
    ("not_an_object", ["damaged", "damaged"]),
])
def test_malformed_model_output_never_decides_anything(direct_vm, direct_deploy, actors, name, payload):
    contract, case = _setup(direct_vm, direct_deploy, actors, payload)
    review(contract, direct_vm, case)
    c = claim_of(contract, case)
    assert c["ruling"] == "" and c["attempts"] == 0
    assert c["reason"] == "observation_failure" and c["review_calls"] == 1
    assert loan_of(contract, case)["status"] == "claimed"


def test_non_json_model_output_and_a_model_outage_are_retryable_failures(
    direct_vm, direct_deploy, actors
):
    contract, case = _setup(direct_vm, direct_deploy, actors, "I rule for the owner.")
    review(contract, direct_vm, case)
    assert claim_of(contract, case)["reason"] == "observation_failure"
    direct_vm.clear_mocks()
    direct_vm.mock_web(case.inv_url, {"status": 200, "body": case.inventory})
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": case.owner_report})
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": case.holder_report})
    direct_vm.warp(at(REVIEW_AT + 60))
    review(contract, direct_vm, case)  # no llm mock at all
    c = claim_of(contract, case)
    assert c["reason"] == "observation_failure" and c["review_calls"] == 2 and c["attempts"] == 0
