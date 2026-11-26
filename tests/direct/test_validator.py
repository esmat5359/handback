"""Equivalence rules: what a validator must reproduce to agree with the leader."""

import json

import pytest

from tests.direct.support import PROMPT_HEAD, RETURN_BY, Case, analysis, deploy, review, to_review


def _leader(direct_vm, direct_deploy, actors, leader_verdict, claimed=(2, 4)):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("val", actors[0], actors[1], verdict=leader_verdict)
    to_review(contract, direct_vm, case, list(claimed))
    review(contract, direct_vm, case)
    return contract, case


def _validator_sees(direct_vm, case, verdict, web=None, holder=True):
    direct_vm.clear_mocks()
    direct_vm.mock_web(case.inv_url, {"status": 200, "body": case.inventory})
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": case.owner_report})
    if holder:
        direct_vm.mock_web(case.holder_url, {"status": 200, "body": case.holder_report})
    for url, response in (web or {}).items():
        direct_vm.mock_web(url, response)
    if verdict is not None:
        direct_vm.mock_llm(PROMPT_HEAD, json.dumps(verdict))


RULED_LEADER = analysis(["damaged", "not_damaged"])


def test_same_ruled_verdict_with_different_prose_and_confidence_agrees(direct_vm, direct_deploy, actors):
    _, case = _leader(direct_vm, direct_deploy, actors, RULED_LEADER)
    _validator_sees(direct_vm, case, analysis(["damaged", "not_damaged"], confidence=81,
                                              rationale="Item two is scored."))
    assert direct_vm.run_validator() is True


@pytest.mark.parametrize("name,verdict", [
    ("opposite_items", analysis(["not_damaged", "damaged"])),
    ("everything_damaged", analysis(["damaged", "damaged"])),
    ("everything_clear", analysis(["not_damaged", "not_damaged"])),
    ("unclear_item", analysis(["damaged", "unclear"])),
    ("low_confidence", analysis(["damaged", "not_damaged"], confidence=60)),
    ("owner_foul", analysis(["damaged", "not_damaged"], owner="yes")),
    ("holder_foul", analysis(["damaged", "not_damaged"], holder="yes")),
    ("unclear_flag", analysis(["damaged", "not_damaged"], holder="unclear")),
])
def test_a_ruled_leader_is_not_confirmed_by_any_other_outcome_or_damaged_set(
    direct_vm, direct_deploy, actors, name, verdict
):
    _, case = _leader(direct_vm, direct_deploy, actors, RULED_LEADER)
    _validator_sees(direct_vm, case, verdict)
    assert direct_vm.run_validator() is False


def test_a_ruled_leader_agrees_when_the_validator_labels_the_same_damaged_set_differently_cleared(
    direct_vm, direct_deploy, actors
):
    _, case = _leader(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged", "not_damaged"]),
                      claimed=(1, 2, 3))
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged", "not_damaged"], confidence=99))
    assert direct_vm.run_validator() is True


def test_an_owner_foul_leader_needs_the_same_foul_class(direct_vm, direct_deploy, actors):
    _, case = _leader(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"], owner="yes"))
    _validator_sees(direct_vm, case, analysis(["not_damaged", "unclear"], owner="yes", confidence=99,
                                              rationale="The owner report addresses the reviewer."))
    assert direct_vm.run_validator() is True  # item labels are irrelevant to a foul ruling
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged"], holder="yes"))
    assert direct_vm.run_validator() is False
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged"]))
    assert direct_vm.run_validator() is False


def test_a_holder_foul_leader_needs_the_same_foul_class(direct_vm, direct_deploy, actors):
    _, case = _leader(direct_vm, direct_deploy, actors, analysis(["damaged", "damaged"], holder="yes"))
    _validator_sees(direct_vm, case, analysis(["not_damaged", "not_damaged"], holder="yes"))
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged"], owner="yes", holder="yes"))
    assert direct_vm.run_validator() is False  # the owner's foul outranks the holder's


def test_an_inconclusive_leader_agrees_with_any_inconclusive_validator_only(
    direct_vm, direct_deploy, actors
):
    _, case = _leader(direct_vm, direct_deploy, actors, analysis(["unclear", "unclear"]))
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged"], confidence=50))
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged"]))
    assert direct_vm.run_validator() is False
    _validator_sees(direct_vm, case, analysis(["damaged", "damaged"], owner="yes"))
    assert direct_vm.run_validator() is False


@pytest.mark.parametrize("url_attr", ["inv_url", "owner_url", "holder_url"])
def test_a_validator_rejects_a_valid_analysis_when_it_cannot_reach_a_document(
    direct_vm, direct_deploy, actors, url_attr
):
    _, case = _leader(direct_vm, direct_deploy, actors, RULED_LEADER)
    url = getattr(case, url_attr)
    _validator_sees(direct_vm, case, RULED_LEADER, web={url: {"status": 503, "body": b""}})
    assert direct_vm.run_validator() is False


@pytest.mark.parametrize("url_attr", ["inv_url", "owner_url", "holder_url"])
def test_a_validator_rejects_a_valid_analysis_when_a_document_hash_differs(
    direct_vm, direct_deploy, actors, url_attr
):
    _, case = _leader(direct_vm, direct_deploy, actors, RULED_LEADER)
    url = getattr(case, url_attr)
    _validator_sees(direct_vm, case, RULED_LEADER, web={url: {"status": 200, "body": b"other"}})
    assert direct_vm.run_validator() is False


def test_a_validator_rejects_a_leader_analysis_when_the_model_returns_garbage(
    direct_vm, direct_deploy, actors
):
    _, case = _leader(direct_vm, direct_deploy, actors, RULED_LEADER)
    direct_vm.clear_mocks()
    direct_vm.mock_web(case.inv_url, {"status": 200, "body": case.inventory})
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": case.owner_report})
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": case.holder_report})
    direct_vm.mock_llm(PROMPT_HEAD, "not json at all")
    assert direct_vm.run_validator() is False


def test_unavailability_is_only_confirmed_by_the_same_side_failing(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("down", actors[0], actors[1], verdict=RULED_LEADER)
    to_review(contract, direct_vm, case, [2, 4])
    direct_vm.mock_web(case.owner_url, {"status": 503, "body": b"down"})
    review(contract, direct_vm, case)  # the leader blames the owner side
    # A validator that can read everything gets an analysis and disagrees.
    _validator_sees(direct_vm, case, RULED_LEADER)
    assert direct_vm.run_validator() is False
    # A validator that is also blocked from the owner's report agrees.
    _validator_sees(direct_vm, case, RULED_LEADER, web={case.owner_url: {"status": 503, "body": b""}})
    assert direct_vm.run_validator() is True
    # A validator blocked only from the holder's report blames the other side: no agreement.
    _validator_sees(direct_vm, case, RULED_LEADER, web={case.holder_url: {"status": 503, "body": b""}})
    assert direct_vm.run_validator() is False


def test_a_holder_side_failure_cannot_be_confirmed_as_an_owner_fault(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("hdown", actors[0], actors[1], verdict=RULED_LEADER)
    to_review(contract, direct_vm, case, [2, 4])
    direct_vm.mock_web(case.holder_url, {"status": 503, "body": b"down"})
    review(contract, direct_vm, case)
    _validator_sees(direct_vm, case, RULED_LEADER, web={case.owner_url: {"status": 503, "body": b""}})
    assert direct_vm.run_validator() is False
    _validator_sees(direct_vm, case, RULED_LEADER, web={case.holder_url: {"status": 503, "body": b""}})
    assert direct_vm.run_validator() is True


def test_tampering_needs_the_same_party_and_the_same_reason(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("tamper", actors[0], actors[1], verdict=RULED_LEADER)
    to_review(contract, direct_vm, case, [2, 4])
    direct_vm.mock_web(case.owner_url, {"status": 200, "body": b"mismatching bytes"})
    review(contract, direct_vm, case)
    _validator_sees(direct_vm, case, RULED_LEADER,
                    web={case.owner_url: {"status": 200, "body": b"mismatching bytes"}})
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, case, RULED_LEADER, web={case.owner_url: {"status": 200, "body": b""}})
    assert direct_vm.run_validator() is False  # an empty body is a different fault
    _validator_sees(direct_vm, case, RULED_LEADER,
                    web={case.holder_url: {"status": 200, "body": b"mismatching bytes"}})
    assert direct_vm.run_validator() is False  # the other party's fault is a different finding
    _validator_sees(direct_vm, case, RULED_LEADER)  # a validator seeing the genuine bytes
    assert direct_vm.run_validator() is False


def test_an_owner_fault_leader_is_not_confirmed_by_a_holder_fault_validator(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("swap", actors[0], actors[1], verdict=RULED_LEADER)
    to_review(contract, direct_vm, case, [2, 4])
    direct_vm.mock_web(case.holder_url, {"status": 200, "body": b"holder swap"})
    review(contract, direct_vm, case)  # holder tampering
    _validator_sees(direct_vm, case, RULED_LEADER,
                    web={case.inv_url: {"status": 200, "body": b"inventory swap"}})
    assert direct_vm.run_validator() is False
    _validator_sees(direct_vm, case, RULED_LEADER,
                    web={case.holder_url: {"status": 200, "body": b"holder swap"}})
    assert direct_vm.run_validator() is True


def test_a_leader_model_failure_is_confirmed_only_by_another_model_failure(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("mf", actors[0], actors[1], verdict="garbage")
    to_review(contract, direct_vm, case, [2, 4])
    review(contract, direct_vm, case)
    _validator_sees(direct_vm, case, None)
    direct_vm.mock_llm(PROMPT_HEAD, "still not json")
    assert direct_vm.run_validator() is True
    _validator_sees(direct_vm, case, RULED_LEADER)
    assert direct_vm.run_validator() is False
