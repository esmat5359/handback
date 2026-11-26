"""Input validation: every malformed offer must revert and leave the books untouched."""

import pytest

from tests.direct.support import (
    ACCEPT_BY, CAPS, CLAIM_WINDOW, DAY, DEPOSIT, INV_URL, INVENTORY, MIN_DEPOSIT, MIN_STAKE,
    NOW_TS, RESPOND_WINDOW, RETURN_BY, STAKE, Case, addr, caps_text, check_books, credit,
    deploy, deposit, digest, loan_of, open_loan,
)

MIN_CAP = 10**13


def _raw_open(contract, vm, owner, holder, **kw):
    params = dict(
        loan_id="L-001", holder=addr(holder), title="Title", summary="Summary", url=INV_URL,
        inv_hash=digest(INVENTORY), count=5, caps=caps_text(CAPS), deposit=DEPOSIT, stake=STAKE,
        accept_by=ACCEPT_BY, return_by=RETURN_BY, claim_window=CLAIM_WINDOW,
        respond_window=RESPOND_WINDOW,
    )
    params.update(kw)
    vm.sender = owner
    return contract.open_loan(
        params["loan_id"], params["holder"], params["title"], params["summary"], params["url"],
        params["inv_hash"], params["count"], params["caps"], params["deposit"], params["stake"],
        params["accept_by"], params["return_by"], params["claim_window"],
        params["respond_window"],
    )


BAD_CASES = {
    "empty_id": dict(loan_id=""),
    "long_id": dict(loan_id="a" * 65),
    "id_with_space": dict(loan_id="bad id"),
    "id_leading_dash": dict(loan_id="-x"),
    "id_hash_char": dict(loan_id="a#b"),
    "id_colon": dict(loan_id="a:b"),
    "empty_title": dict(title="   "),
    "long_title": dict(title="t" * 121),
    "empty_summary": dict(summary=""),
    "long_summary": dict(summary="s" * 601),
    "holder_garbage": dict(holder="not-an-address"),
    "holder_zero": dict(holder="0x" + "0" * 40),
    "holder_empty": dict(holder=""),
    "short_hash": dict(inv_hash="0x1234"),
    "no_prefix_hash": dict(inv_hash="ab" * 32),
    "non_hex_hash": dict(inv_hash="0x" + "g" * 64),
    "http_url": dict(url="http://loans.example.org/i.md"),
    "localhost": dict(url="https://localhost/i.md"),
    "ipv4": dict(url="https://127.0.0.1/i.md"),
    "private_ip": dict(url="https://10.0.0.5/i.md"),
    "ipv6": dict(url="https://[::1]/i.md"),
    "credentials": dict(url="https://user:pw@loans.example.org/i.md"),
    "odd_port": dict(url="https://loans.example.org:8443/i.md"),
    "fragment": dict(url="https://loans.example.org/i.md#x"),
    "dot_local": dict(url="https://printer.local/i.md"),
    "dot_internal": dict(url="https://svc.internal/i.md"),
    "single_label": dict(url="https://intranet/i.md"),
    "space_in_url": dict(url="https://loans.example.org/a b.md"),
    "control_in_url": dict(url="https://loans.example.org/a\x01b.md"),
    "long_url": dict(url="https://loans.example.org/" + "a" * 520),
    "zero_items": dict(count=0, caps=""),
    "too_many_items": dict(count=9, caps=caps_text([MIN_CAP] * 8) + "," + str(MIN_CAP)),
    "u16_overflow_items": dict(count=70_000),
    "item_count_mismatch_fewer_caps": dict(count=5, caps=caps_text(CAPS[:4])),
    "item_count_mismatch_more_caps": dict(count=4, caps=caps_text(CAPS)),
    "deposit_below_min": dict(deposit=MIN_DEPOSIT - 1),
    "stake_below_min": dict(stake=MIN_STAKE - 1),
    "cap_zero": dict(caps="1000000000000000,0,500000000000000,400000000000000,300000000000000"),
    "cap_negative": dict(caps="1000000000000000,-600000000000000,500000000000000,400000000000000,300000000000000"),
    "cap_leading_zero": dict(caps="01000000000000000,600000000000000,500000000000000,400000000000000,300000000000000"),
    "cap_decimal": dict(caps="1000000000000000,6e14,500000000000000,400000000000000,300000000000000"),
    "cap_float": dict(caps="1000000000000000,600000000000000.5,500000000000000,400000000000000,300000000000000"),
    "cap_empty_entry": dict(caps="1000000000000000,,500000000000000,400000000000000,300000000000000"),
    "cap_below_min": dict(caps=caps_text([MIN_CAP - 1, *CAPS[1:]])),
    "cap_huge": dict(caps=caps_text([10**40, *CAPS[1:]])),
    "caps_exceed_deposit": dict(caps=caps_text([*CAPS[:-1], CAPS[-1] + (DEPOSIT - sum(CAPS)) + 1])),
    "caps_empty": dict(caps=""),
    "accept_too_soon": dict(accept_by=NOW_TS + 119),
    "accept_in_past": dict(accept_by=NOW_TS - 1),
    "accept_too_far": dict(accept_by=NOW_TS + 30 * DAY + 1, return_by=NOW_TS + 31 * DAY),
    "return_too_soon": dict(return_by=ACCEPT_BY + 119),
    "return_before_accept": dict(return_by=ACCEPT_BY - 1),
    "return_too_far": dict(return_by=ACCEPT_BY + 90 * DAY + 1),
    "claim_window_too_short": dict(claim_window=119),
    "claim_window_too_long": dict(claim_window=14 * DAY + 1),
    "claim_window_zero": dict(claim_window=0),
    "respond_window_too_short": dict(respond_window=119),
    "respond_window_too_long": dict(respond_window=7 * DAY + 1),
    "respond_window_zero": dict(respond_window=0),
}


@pytest.mark.parametrize("case", sorted(BAD_CASES))
def test_malformed_offer_reverts_without_touching_the_books(direct_vm, direct_deploy, actors, case):
    owner, holder = actors[0], actors[1]
    contract = deploy(direct_vm, direct_deploy)
    deposit(contract, direct_vm, owner, 10 * STAKE)
    with direct_vm.expect_revert():
        _raw_open(contract, direct_vm, owner, holder, **BAD_CASES[case])
    assert credit(contract, owner) == 10 * STAKE
    accounting = contract.get_accounting()
    assert accounting["total_locked_stakes"] == 0
    assert contract.get_loan_count() == 0
    # Control: the same call with every field valid succeeds, so the revert above was
    # caused by the single bad field.
    _raw_open(contract, direct_vm, owner, holder)
    assert loan_of(contract, Case("c", owner, holder, loan_id="L-001"))["status"] == "offered"
    assert contract.get_loan_count() == 1


def test_owner_cannot_lend_to_itself(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    deposit(contract, direct_vm, actors[0], STAKE)
    with direct_vm.expect_revert("Owner cannot lend to itself"):
        _raw_open(contract, direct_vm, actors[0], actors[0])


def test_offer_needs_the_stake_in_credit(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    deposit(contract, direct_vm, actors[0], STAKE - 1)
    with direct_vm.expect_revert("Insufficient available credit"):
        _raw_open(contract, direct_vm, actors[0], actors[1])
    assert credit(contract, actors[0]) == STAKE - 1


def test_duplicate_loan_id_for_the_same_owner_reverts_but_other_owners_may_reuse_it(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    deposit(contract, direct_vm, actors[0], 3 * STAKE)
    deposit(contract, direct_vm, actors[2], STAKE)
    _raw_open(contract, direct_vm, actors[0], actors[1])
    with direct_vm.expect_revert("Duplicate loan id"):
        _raw_open(contract, direct_vm, actors[0], actors[3])
    _raw_open(contract, direct_vm, actors[2], actors[1])
    assert contract.get_loan_count() == 2
    assert credit(contract, actors[0]) == 2 * STAKE


@pytest.mark.parametrize("count", [1, 8])
def test_item_count_boundaries_are_inclusive(direct_vm, direct_deploy, actors, count):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("b", actors[0], actors[1])
    caps = [MIN_CAP] * count
    open_loan(contract, direct_vm, case, count=count, caps=caps)
    assert loan_of(contract, case)["item_count"] == count
    assert loan_of(contract, case)["caps"] == caps


def test_exact_minimums_maximums_and_cap_sum_equal_to_deposit_are_accepted(
    direct_vm, direct_deploy, actors
):
    contract = deploy(direct_vm, direct_deploy)
    direct_vm.warp("2026-10-01T12:00:00Z")
    accept_by = NOW_TS + 120
    case = Case("edge", actors[0], actors[1])
    open_loan(contract, direct_vm, case, deposit_wei=MIN_DEPOSIT, stake=MIN_STAKE,
              caps=[MIN_DEPOSIT - 4 * MIN_CAP] + [MIN_CAP] * 4, accept_by=accept_by,
              return_by=accept_by + 120, claim_window=120, respond_window=120)
    loan = loan_of(contract, case)
    assert sum(loan["caps"]) == loan["deposit_wei"] == MIN_DEPOSIT
    other = Case("edge2", actors[2], actors[3], loan_id="L-002")
    open_loan(contract, direct_vm, other, accept_by=NOW_TS + 30 * DAY,
              return_by=NOW_TS + 30 * DAY + 90 * DAY, claim_window=14 * DAY,
              respond_window=7 * DAY)
    assert loan_of(contract, other)["claim_window"] == 14 * DAY


def test_title_and_summary_whitespace_is_normalised(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("ws", actors[0], actors[1])
    open_loan(contract, direct_vm, case, title="  Photo   kit\n loan ", summary="A\x00 kit")
    loan = loan_of(contract, case)
    assert loan["title"] == "Photo kit loan"
    assert loan["summary"] == "A kit"


def test_offer_records_terms_and_locks_only_the_stake(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    case = Case("terms", actors[0], actors[1])
    open_loan(contract, direct_vm, case)
    loan = loan_of(contract, case)
    assert loan["owner"].lower() == addr(actors[0])
    assert loan["holder"].lower() == addr(actors[1])
    assert loan["caps"] == CAPS and loan["caps_total"] == sum(CAPS)
    assert loan["deposit_wei"] == DEPOSIT and loan["stake_wei"] == STAKE
    assert loan["inventory_hash"] == digest(INVENTORY)
    assert (loan["accept_by"], loan["return_by"]) == (ACCEPT_BY, RETURN_BY)
    assert loan["claim_by"] == RETURN_BY + CLAIM_WINDOW
    accounting = check_books(contract, actors, [case])
    assert accounting["total_locked_stakes"] == STAKE
    assert accounting["total_locked_deposits"] == 0
    ref = contract.get_loan_ref(0)
    assert ref["owner"].lower() == addr(actors[0]) and ref["loan_id"] == "L-001"
    with direct_vm.expect_revert("out of range"):
        contract.get_loan_ref(1)


def test_views_reject_unknown_and_malformed_references(direct_vm, direct_deploy, actors):
    contract = deploy(direct_vm, direct_deploy)
    with direct_vm.expect_revert("Loan not found"):
        contract.get_loan(addr(actors[0]), "nope")
    with direct_vm.expect_revert("Invalid owner"):
        contract.get_loan("garbage", "L-001")
    with direct_vm.expect_revert("Invalid loan id"):
        contract.get_loan(addr(actors[0]), "bad id")
    with direct_vm.expect_revert("Invalid credit owner"):
        contract.get_available_credit("garbage")
