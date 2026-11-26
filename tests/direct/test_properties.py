"""Exhaustive payout tables and a seeded random walk over the whole state machine."""

import itertools
import json
import random

import pytest

from tests.direct.support import (
    CAPS, DEPOSIT, ITEM_COUNT, NOW_TS, RETURN_BY, STAKE, Case, accept, addr, analysis, at,
    caps_text, check_books, claim, claim_of, credit, deploy, deposit, deduction, digest,
    hand_back, loan_of, mask_of, open_loan, respond, review, serve, settle,
)


def _items(mask):
    return [position + 1 for position in range(8) if (mask >> position) & 1]


def test_every_claim_and_acceptance_combination_pays_exactly_the_agreed_caps(
    direct_vm, direct_deploy, actors
):
    """For all 5-item claims, all acceptance subsets and both extreme verdicts."""
    contract = deploy(direct_vm, direct_deploy)
    owner, holder = actors[0], actors[1]
    counter = 0
    expected_owner = expected_holder = 0
    cases = []
    for claimed in range(1, 1 << ITEM_COUNT):
        subsets = [s for s in range(claimed + 1) if s & ~claimed == 0]
        for accepted in subsets:
            disputed = claimed & ~accepted
            verdicts = ["none"] if disputed == 0 else ["all_damaged", "all_clear"]
            for kind in verdicts:
                counter += 1
                label = ["damaged"] * bin(disputed).count("1") if kind == "all_damaged" else \
                    ["not_damaged"] * bin(disputed).count("1")
                case = Case(f"x{counter}", owner, holder, loan_id=f"X{counter}",
                            verdict=analysis(label) if disputed else None)
                cases.append(case)
                open_loan(contract, direct_vm, case)
                direct_vm.warp(at(NOW_TS + 60))
                accept(contract, direct_vm, case)
                direct_vm.warp(at(RETURN_BY))
                claim(contract, direct_vm, case, _items(claimed))
                respond(contract, direct_vm, case, _items(accepted))
                if disputed:
                    serve(direct_vm, [case])
                    review(contract, direct_vm, case)
                    settle(contract, direct_vm, actors[9], case)
                deducted_mask = accepted | (disputed if kind == "all_damaged" else 0)
                deducted = sum(CAPS[i - 1] for i in _items(deducted_mask))
                loan = loan_of(contract, case)
                assert loan["status"] == "settled"
                assert loan["deducted_mask"] == deducted_mask
                assert loan["deducted_wei"] == deducted
                assert loan["returned_wei"] == DEPOSIT - deducted
                expected_owner += deducted + STAKE
                expected_holder += DEPOSIT - deducted
                direct_vm.warp(at(NOW_TS))  # rewind for the next loan
    assert credit(contract, owner) == expected_owner
    assert credit(contract, holder) == expected_holder
    accounting = check_books(contract, actors, cases)
    assert accounting["total_locked_deposits"] == 0 and accounting["total_locked_stakes"] == 0
    assert counter > 400


VALID_TRANSITIONS = {
    "offered": {"offered", "active", "cancelled"},
    "active": {"active", "claimed", "settled"},
    "claimed": {"claimed", "settled"},
    "settled": {"settled"},
    "cancelled": {"cancelled"},
}
def _closure(graph):
    reach = {k: set(v) for k, v in graph.items()}
    changed = True
    while changed:
        changed = False
        for k in reach:
            extra = set().union(*(reach[n] for n in list(reach[k]))) - reach[k]
            if extra:
                reach[k] |= extra
                changed = True
    return reach


REACHABLE = _closure(VALID_TRANSITIONS)
SETTLEMENTS = {
    "offer_cancelled", "offer_expired", "no_claim_deposit_returned", "claim_accepted_by_holder",
    "review_ruled", "review_owner_foul", "review_holder_foul", "review_owner_evidence_tampered",
    "review_holder_evidence_tampered", "review_owner_evidence_unavailable",
    "review_holder_evidence_unavailable", "review_burden_unmet", "review_claim_withdrawn",
    "released_by_owner",
}


class Walk:
    def __init__(self, vm, contract, actors, rng):
        self.vm, self.contract, self.actors, self.rng = vm, contract, actors, rng
        self.owners = actors[:2]
        self.holders = actors[2:5]
        self.cases = []
        self.clock = NOW_TS
        self.in_flight = 0   # total ever deposited
        self.out_flight = 0  # total ever withdrawn
        self.statuses = {}
        self.reverts = 0

    # -- helpers -----------------------------------------------------------
    def attempt(self, call):
        try:
            call()
            return True
        except Exception:
            self.reverts += 1
            return False

    def warp(self, seconds):
        self.clock += seconds
        self.vm.warp(at(self.clock))

    def pick(self, statuses):
        pool = [c for c in self.cases if loan_of(self.contract, c)["status"] in statuses]
        return self.rng.choice(pool) if pool else None

    # -- actions -----------------------------------------------------------
    def a_deposit(self):
        who = self.rng.choice(self.owners + self.holders)
        amount = self.rng.randint(1, 3) * 10**14 * self.rng.randint(1, 20)
        self.vm.sender = who
        self.vm.value = amount
        if self.attempt(lambda: self.contract.deposit_credit()):
            self.in_flight += amount
        self.vm.value = 0

    def a_open(self):
        owner = self.rng.choice(self.owners)
        holder = self.rng.choice(self.holders)
        count = self.rng.randint(1, 8)
        deposit_wei = self.rng.randint(2, 6) * 10**15
        caps = [self.rng.randint(1, 10) * 10**13 for _ in range(count)]
        if sum(caps) > deposit_wei:
            caps = [10**13] * count
        index = len(self.cases)
        case = Case(f"w{index}", owner, holder, loan_id=f"W{index}")
        self.vm.sender = owner
        accept_by = self.clock + self.rng.randint(120, 1800)
        return_by = accept_by + self.rng.randint(120, 3600)
        claim_window = self.rng.randint(120, 1800)
        respond_window = self.rng.randint(120, 1800)
        ok = self.attempt(lambda: self.contract.open_loan(
            case.loan_id, addr(holder), "Fuzz loan", "A fuzzed loan.", case.inv_url,
            digest(case.inventory), count, caps_text(caps), deposit_wei, STAKE, accept_by,
            return_by, claim_window, respond_window))
        if ok:
            case.extra = {"count": count}
            self.cases.append(case)

    def a_accept(self):
        case = self.pick({"offered"})
        if case is None:
            return
        self.vm.sender = case.holder
        self.attempt(lambda: self.contract.accept_loan(addr(case.owner), case.loan_id,
                                                       digest(case.inventory)))

    def a_hand_back(self):
        case = self.pick({"active"})
        if case is None:
            return
        self.vm.sender = case.holder
        with_report = self.rng.random() < 0.7
        self.attempt(lambda: self.contract.hand_back(
            addr(case.owner), case.loan_id,
            case.holder_url if with_report else "", case.holder_hash if with_report else ""))

    def a_claim(self):
        case = self.pick({"active"})
        if case is None:
            return
        count = case.extra["count"]
        mask = self.rng.randint(1, (1 << count) - 1)
        self.vm.sender = case.owner
        self.attempt(lambda: self.contract.file_claim(case.loan_id, mask, case.owner_url,
                                                      case.owner_hash))

    def a_respond(self):
        case = self.pick({"claimed"})
        if case is None:
            return
        claimed = claim_of(self.contract, case)["claimed_mask"]
        accepted = claimed & self.rng.getrandbits(8)
        if self.rng.random() < 0.1:
            accepted |= 1 << 7  # sometimes outside the claim: must revert
        with_report = self.rng.random() < 0.3
        self.vm.sender = case.holder
        self.attempt(lambda: self.contract.respond_to_claim(
            addr(case.owner), case.loan_id, accepted,
            case.holder_url if with_report else "", case.holder_hash if with_report else ""))

    def a_review(self):
        case = self.pick({"claimed"})
        if case is None:
            return
        c = claim_of(self.contract, case)
        count = len(c["disputed_items"])
        if count == 0:
            return
        mode = self.rng.choice(["ok", "ok", "ok", "mixed", "owner_down", "holder_down",
                                "owner_swap", "holder_swap", "garbage", "foul"])
        labels = ["damaged", "not_damaged", "unclear"]
        weights = [4, 4, 1] if mode == "mixed" else [5, 5, 0]
        items = self.rng.choices(labels, weights, k=count)
        owner_flag = "yes" if mode == "foul" and self.rng.random() < 0.5 else "no"
        holder_flag = "yes" if mode == "foul" and owner_flag == "no" else "no"
        verdict = analysis(items, owner=owner_flag, holder=holder_flag,
                           confidence=self.rng.choice([50, 79, 80, 95]))
        if mode == "garbage":
            verdict = {"items": "garbage"}
        case.verdict = verdict
        self.vm.clear_mocks()
        serve(self.vm, [case])
        if mode == "owner_down":
            self.vm.mock_web(case.owner_url, {"status": 503, "body": b"down"})
        elif mode == "holder_down":
            self.vm.mock_web(case.holder_url, {"status": 503, "body": b"down"})
        elif mode == "owner_swap":
            self.vm.mock_web(case.owner_url, {"status": 200, "body": b"swapped"})
        elif mode == "holder_swap":
            self.vm.mock_web(case.holder_url, {"status": 200, "body": b"swapped"})
        self.vm.sender = self.rng.choice([case.owner, case.holder, self.actors[9]])
        self.attempt(lambda: self.contract.review_claim(addr(case.owner), case.loan_id))

    def a_settle(self):
        case = self.pick({"active", "claimed"})
        if case is None:
            return
        self.vm.sender = self.rng.choice(self.actors[:10])
        self.attempt(lambda: self.contract.settle_loan(addr(case.owner), case.loan_id))

    def a_withdraw_claim(self):
        case = self.pick({"claimed"})
        if case is None:
            return
        self.vm.sender = case.owner
        self.attempt(lambda: self.contract.withdraw_claim(case.loan_id))

    def a_release(self):
        case = self.pick({"active"})
        if case is None:
            return
        self.vm.sender = case.owner
        self.attempt(lambda: self.contract.release_loan(case.loan_id))

    def a_cancel(self):
        case = self.pick({"offered", "active"})
        if case is None:
            return
        self.vm.sender = case.owner
        self.attempt(lambda: self.contract.cancel_offer(case.loan_id))

    def a_expire(self):
        case = self.pick({"offered"})
        if case is None:
            return
        self.vm.sender = self.actors[9]
        self.attempt(lambda: self.contract.expire_offer(addr(case.owner), case.loan_id))

    def a_withdraw_credit(self):
        who = self.rng.choice(self.owners + self.holders)
        before = credit(self.contract, who)
        self.vm.sender = who
        if self.attempt(lambda: self.contract.withdraw_credit()):
            self.out_flight += before

    def a_drive(self):
        """Take the natural next step for a random live loan so deep states get reached."""
        case = self.pick({"offered", "active", "claimed"})
        if case is None:
            return
        loan = loan_of(self.contract, case)
        c = claim_of(self.contract, case)
        status = loan["status"]
        if status == "offered":
            if self.clock >= loan["accept_by"]:
                self.a_expire()
                return
            need = loan["deposit_wei"] - credit(self.contract, case.holder)
            if need > 0:
                self.vm.sender = case.holder
                self.vm.value = need
                if self.attempt(lambda: self.contract.deposit_credit()):
                    self.in_flight += need
                self.vm.value = 0
            self.vm.sender = case.holder
            self.attempt(lambda: self.contract.accept_loan(addr(case.owner), case.loan_id,
                                                           digest(case.inventory)))
        elif status == "active":
            if loan["returned_at"] == 0 and self.clock < loan["return_by"]:
                self.a_hand_back()
            elif self.rng.random() < 0.85:
                self.a_claim()
            else:
                self.a_settle()
        elif status == "claimed":
            if c["responded_at"] == 0 and self.clock < c["respond_by"] and self.rng.random() < 0.7:
                self.a_respond()
            elif c["ruling"] == "" and self.rng.random() < 0.8:
                self.a_review()
            else:
                self.a_settle()

    def a_warp(self):
        self.warp(self.rng.choice([30, 60, 120, 600, 1200, 3600, 7200, 4 * 86400]))

    def a_advance(self):
        """Jump the clock to (or one second around) a deadline of a live loan."""
        case = self.pick({"offered", "active", "claimed"})
        if case is None:
            return
        loan = loan_of(self.contract, case)
        c = claim_of(self.contract, case)
        marks = [loan["accept_by"], loan["return_by"], loan["claim_by"], c["respond_by"],
                 c["review_start"], c["review_by"], c["retry_at"]]
        marks = [m for m in marks if m > self.clock]
        if not marks:
            return
        target = min(marks) if self.rng.random() < 0.6 else self.rng.choice(marks)
        target += self.rng.choice([-1, 0, 0, 1])
        if target > self.clock:
            self.warp(target - self.clock)

    # -- invariants --------------------------------------------------------
    def check(self):
        accounting = check_books(self.contract, self.actors, self.cases)
        assert accounting["total_credits"] + accounting["total_locked_deposits"] + \
            accounting["total_locked_stakes"] == self.in_flight - self.out_flight, \
            "value in must equal credits plus everything locked plus nothing else"
        for case in self.cases:
            loan = loan_of(self.contract, case)
            claim_state = claim_of(self.contract, case)
            before = self.statuses.get(case.name, "offered")
            assert loan["status"] in REACHABLE[before], (before, loan["status"])
            self.statuses[case.name] = loan["status"]
            assert claim_state["review_calls"] <= 8 and claim_state["attempts"] <= 3
            assert claim_state["ruled_mask"] & ~claim_state["disputed_mask"] == 0
            assert claim_state["accepted_mask"] & ~claim_state["claimed_mask"] == 0
            if claim_state["ruling"]:
                assert loan["status"] in ("claimed", "settled")
            if loan["status"] in ("settled", "cancelled"):
                assert loan["settlement"] in SETTLEMENTS, loan["settlement"]
            if loan["status"] == "settled":
                assert loan["deducted_wei"] + loan["returned_wei"] == loan["deposit_wei"]
                assert loan["deducted_wei"] == sum(
                    loan["caps"][i - 1] for i in _items(loan["deducted_mask"]))
                assert loan["deducted_mask"] & ~claim_state["claimed_mask"] == 0
                assert loan["stake_forfeited_wei"] in (0, loan["stake_wei"])
                if loan["stake_forfeited_wei"]:
                    assert claim_state["ruling"] == "owner_evidence_tampered"
                if claim_state["ruling"] in ("owner_foul", "owner_evidence_tampered",
                                             "owner_evidence_unavailable", "burden_unmet",
                                             "claim_withdrawn"):
                    assert loan["deducted_mask"] == claim_state["accepted_mask"]
                if claim_state["ruling"] in ("holder_foul", "holder_evidence_tampered",
                                             "holder_evidence_unavailable"):
                    assert loan["deducted_mask"] == claim_state["claimed_mask"]
                if loan["settlement"] in ("no_claim_deposit_returned", "released_by_owner"):
                    assert loan["deducted_wei"] == 0
            if loan["status"] == "cancelled":
                assert loan["deducted_wei"] == 0 and loan["accepted_at"] == 0
            assert loan["caps_total"] <= loan["deposit_wei"]

    def run(self, steps):
        actions = [
            (self.a_deposit, 6), (self.a_open, 6), (self.a_accept, 5), (self.a_hand_back, 3),
            (self.a_claim, 5), (self.a_respond, 4), (self.a_review, 8), (self.a_settle, 4),
            (self.a_withdraw_claim, 1), (self.a_release, 2), (self.a_cancel, 1), (self.a_expire, 2),
            (self.a_withdraw_credit, 2), (self.a_warp, 3), (self.a_advance, 12), (self.a_drive, 30),
        ]
        population = [fn for fn, weight in actions for _ in range(weight)]
        for step in range(steps):
            self.rng.choice(population)()
            if step % 6 == 5:
                self.check()
        self.check()


def _seeds():
    """Ten fixed seeds by default; HANDBACK_FUZZ_SEEDS=11-60 widens the search locally."""
    import os
    spec = os.environ.get("HANDBACK_FUZZ_SEEDS", "1-10")
    low, _, high = spec.partition("-")
    return list(range(int(low), int(high or low) + 1))


@pytest.mark.parametrize("seed", _seeds())
def test_random_walks_keep_every_invariant(direct_vm, direct_deploy, actors, seed):
    contract = deploy(direct_vm, direct_deploy)
    walk = Walk(direct_vm, contract, actors, random.Random(seed))
    # Everyone starts with some credit so offers and acceptances can actually happen.
    for _ in range(12):
        walk.a_deposit()
    walk.run(220)
    settled = sum(1 for c in walk.cases if loan_of(contract, c)["status"] == "settled")
    assert walk.cases, "the walk must create loans"
    rulings = sorted({claim_of(contract, c)["ruling"] or "-" for c in walk.cases})
    print(f"seed={seed} loans={len(walk.cases)} settled={settled} reverts={walk.reverts} "
          f"rulings={rulings}")
