"""Shared scenario helpers for the Handback Direct Mode tests."""

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.handback_tools import caps_text, deduction, mask_of  # noqa: E402,F401
from scripts.handback_tools import sha256_hex  # noqa: E402

CONTRACT = "contracts/handback.py"
RUNNER = "v0.2.16"
NOW = "2026-10-01T12:00:00Z"
NOW_TS = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())
HOUR = 3600
DAY = 24 * HOUR

CAPS = [10**15, 6 * 10**14, 5 * 10**14, 4 * 10**14, 3 * 10**14]
ITEM_COUNT = len(CAPS)
DEPOSIT = 3 * 10**15
STAKE = 3 * 10**14
MIN_DEPOSIT = 10**15
MIN_STAKE = 10**14
ACCEPT_BY = NOW_TS + HOUR
RETURN_BY = ACCEPT_BY + 2 * HOUR
CLAIM_WINDOW = HOUR
RESPOND_WINDOW = HOUR
REVIEW_WINDOW = 3 * DAY
LOAN_ID = "L-001"

INV_URL = "https://loans.example.org/inventory.md"
INVENTORY = (
    b"# Handover inventory\n"
    b"I1: Camera body - scratch-free, shutter works.\n"
    b"I2: 24-70mm lens - clean glass, smooth zoom ring.\n"
    b"I3: Tripod - one small paint chip on a leg.\n"
    b"I4: Flash unit - works, battery cover intact.\n"
    b"I5: Camera bag - clean, zipper works.\n"
)

# Every review prompt contains exactly one marker line that mocks can key on.
PROMPT_HEAD = r"(?s)You are independently reviewing a disputed deposit claim"
CONFIDENT = 92


def at(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def addr(value):
    return "0x" + value.hex().lower().removeprefix("0x")


def digest(raw):
    return sha256_hex(raw)


def analysis(items, owner="no", holder="no", confidence=CONFIDENT,
             rationale="Each disputed item was compared with the inventory."):
    return {
        "items": list(items),
        "owner_instruction_attempt": owner,
        "holder_instruction_attempt": holder,
        "confidence": confidence,
        "rationale": rationale,
    }


@dataclass
class Case:
    """One loan: its parties, its evidence, and what the model will say about it."""

    name: str
    owner: bytes
    holder: bytes
    loan_id: str = LOAN_ID
    verdict: object = None
    owner_report: bytes = b""
    holder_report: bytes = b""
    owner_url: str = ""
    holder_url: str = ""
    owner_hash: str = ""
    holder_hash: str = ""
    inventory: bytes = INVENTORY
    inv_url: str = INV_URL
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.owner_report:
            self.owner_report = (
                f"OWNERREPORT-{self.name.upper()}: on return the lens zoom ring is "
                f"stiff and the flash cover is cracked.\n"
            ).encode()
        if not self.holder_report:
            self.holder_report = (
                f"HOLDERREPORT-{self.name.upper()}: everything was returned in the same "
                f"condition as at handover.\n"
            ).encode()
        if not self.owner_url:
            self.owner_url = f"https://loans.example.org/{self.name}-owner.md"
        if not self.holder_url:
            self.holder_url = f"https://loans.example.org/{self.name}-holder.md"
        if not self.owner_hash:
            self.owner_hash = digest(self.owner_report)
        if not self.holder_hash:
            self.holder_hash = digest(self.holder_report)

    @property
    def marker(self):
        return f"OWNERREPORT-{self.name.upper()}:"

    def verdict_json(self):
        return json.dumps(self.verdict)


def deploy(direct_vm, direct_deploy):
    direct_vm.warp(NOW)
    return direct_deploy(CONTRACT, sdk_version=RUNNER)


def deposit(contract, direct_vm, who, amount):
    direct_vm.sender = who
    direct_vm.value = amount
    assert contract.deposit_credit() == amount
    direct_vm.value = 0


def open_loan(contract, direct_vm, case, *, count=ITEM_COUNT, caps=None, deposit_wei=DEPOSIT,
              stake=STAKE, accept_by=ACCEPT_BY, return_by=RETURN_BY, claim_window=CLAIM_WINDOW,
              respond_window=RESPOND_WINDOW, title="Photo kit loan",
              summary="A camera kit lent for a weekend shoot.", url=None, inv_hash=None,
              fund=True, holder=None):
    if fund:
        deposit(contract, direct_vm, case.owner, stake)
    direct_vm.sender = case.owner
    return contract.open_loan(
        case.loan_id, addr(case.holder if holder is None else holder), title, summary,
        url if url is not None else case.inv_url,
        inv_hash if inv_hash is not None else digest(case.inventory),
        count, caps_text(CAPS if caps is None else caps), deposit_wei, stake,
        accept_by, return_by, claim_window, respond_window,
    )


def accept(contract, direct_vm, case, topup=True, deposit_wei=DEPOSIT, inv_hash=None):
    if topup:
        deposit(contract, direct_vm, case.holder, deposit_wei)
    direct_vm.sender = case.holder
    contract.accept_loan(addr(case.owner), case.loan_id,
                         inv_hash if inv_hash is not None else digest(case.inventory))


def hand_back(contract, direct_vm, case, with_report=True):
    direct_vm.sender = case.holder
    if with_report:
        contract.hand_back(addr(case.owner), case.loan_id, case.holder_url, case.holder_hash)
    else:
        contract.hand_back(addr(case.owner), case.loan_id, "", "")


def claim(contract, direct_vm, case, items, url=None, report_hash=None):
    direct_vm.sender = case.owner
    contract.file_claim(
        case.loan_id, mask_of(items),
        url if url is not None else case.owner_url,
        report_hash if report_hash is not None else case.owner_hash,
    )


def respond(contract, direct_vm, case, accepted_items=(), with_report=False):
    direct_vm.sender = case.holder
    if with_report:
        contract.respond_to_claim(addr(case.owner), case.loan_id, mask_of(list(accepted_items)),
                                  case.holder_url, case.holder_hash)
    else:
        contract.respond_to_claim(addr(case.owner), case.loan_id, mask_of(list(accepted_items)),
                                  "", "")


def serve(direct_vm, cases, with_holder=True):
    """Mock the web and the model for every case. Patterns are mutually exclusive."""
    for case in cases:
        direct_vm.mock_web(case.inv_url, {"status": 200, "body": case.inventory})
        direct_vm.mock_web(case.owner_url, {"status": 200, "body": case.owner_report})
        if with_holder:
            direct_vm.mock_web(case.holder_url, {"status": 200, "body": case.holder_report})
        if case.verdict is not None:
            direct_vm.mock_llm(
                PROMPT_HEAD + r".*BEGIN_UNTRUSTED_OWNER_REPORT\n[^\n]*" + case.marker,
                case.verdict_json(),
            )


def review(contract, direct_vm, case, caller=None):
    direct_vm.sender = caller if caller is not None else case.owner
    contract.review_claim(addr(case.owner), case.loan_id)


def settle(contract, direct_vm, caller, case):
    direct_vm.sender = caller
    contract.settle_loan(addr(case.owner), case.loan_id)


def credit(contract, who):
    return contract.get_available_credit(addr(who))


def loan_of(contract, case):
    return contract.get_loan(addr(case.owner), case.loan_id)


def claim_of(contract, case):
    return contract.get_claim(addr(case.owner), case.loan_id)


def to_review(contract, direct_vm, case, claimed, accepted=(), *, holder_report=True,
              with_hand_back=True, respond_with_report=False, **loan_kwargs):
    """Open, accept, return, claim and respond; leave the clock at the start of review.

    The holder's report is committed at hand-back by default so the response only
    chooses which claimed items to accept.
    """
    open_loan(contract, direct_vm, case, **loan_kwargs)
    direct_vm.warp(at(NOW_TS + 60))
    accept(contract, direct_vm, case)
    if with_hand_back:
        hand_back(contract, direct_vm, case, with_report=holder_report)
    direct_vm.warp(at(RETURN_BY))
    claim(contract, direct_vm, case, claimed)
    direct_vm.warp(at(RETURN_BY + 60))
    respond(contract, direct_vm, case, accepted, with_report=respond_with_report)
    serve(direct_vm, [case], with_holder=holder_report or respond_with_report)


def check_books(contract, addresses, loans=()):
    """Assert the whole ledger is internally consistent.

    addresses: every account that may hold credit.
    loans: Case objects whose escrow may still be locked.
    """
    accounting = contract.get_accounting()
    held = sum(credit(contract, who) for who in addresses)
    assert held == accounting["total_credits"], "per-account credits must sum to the aggregate"
    deposits = 0
    stakes = 0
    for case in loans:
        loan = loan_of(contract, case)
        if loan["status"] in ("active", "claimed"):
            deposits += loan["deposit_wei"]
        if loan["status"] in ("offered", "active", "claimed"):
            stakes += loan["stake_wei"]
    assert deposits == accounting["total_locked_deposits"], "locked deposits must match live loans"
    assert stakes == accounting["total_locked_stakes"], "locked stakes must match live loans"
    assert accounting["accounted_liabilities"] == (
        accounting["total_credits"] + accounting["total_locked_deposits"]
        + accounting["total_locked_stakes"]
    )
    return accounting
