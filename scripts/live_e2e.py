#!/usr/bin/env python3
"""End-to-end live test of Handback on GenLayer Studionet, with a written report.

Run it from a GitHub Codespace (or any machine with internet access):

    python scripts/live_e2e.py --address 0xYourDeployedHandbackAddress

It drives six loans through the real network with real validators and a real LLM, all
between the same owner and holder so one run shows every branch of the state machine:

  A  mixed claim      holder accepts one item, validators rule on the three disputed ones
  B  steering         the owner's report tries to instruct the reviewer: items are voided
  C  tampering        the owner's hosted report differs from its commitment: stake forfeited
  D  no claim         nobody claims; the deposit comes back after the claim window
  E  early release    the owner releases the deposit right after the return
  F  silent holder    the holder never responds; the claim is reviewed on the owner's evidence

It then withdraws every credit as native GEN and compares wallet balances with the ledger.
Everything it asserts is read back from the contract, not inferred from receipts. A Markdown
and a JSON report are written to ./reports/.

The fixture documents under fixtures/live/ must be reachable over public HTTPS because the
validators fetch them. The script derives commit-pinned raw.githubusercontent.com URLs from the
current git checkout, verifies that each one serves exactly the local bytes, and refuses to
start otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.handback_tools import caps_text, items_of, mask_of, sha256_hex  # noqa: E402

CONTRACT_PATH = ROOT / "contracts" / "handback.py"
FIXTURE_DIR = ROOT / "fixtures" / "live"
REPORT_DIR = ROOT / "reports"
STATE_DIR = ROOT / ".live"
EXPLORER_TX = "https://explorer-studio.genlayer.com/tx/"
EXPLORER_ADDRESS = "https://explorer-studio.genlayer.com/address/"
DEFAULT_RPC = "https://studio.genlayer.com/api"
GEN = 10**18

CAPS = [400_000_000_000_000, 300_000_000_000_000, 250_000_000_000_000,
        200_000_000_000_000, 150_000_000_000_000]            # 0.0004 .. 0.00015 GEN per item
DEPOSIT = 2_000_000_000_000_000                              # 0.002 GEN per loan
STAKE = 300_000_000_000_000                                  # 0.0003 GEN per offer
LOANS = ["A", "B", "C", "D", "E", "F"]
MIN_RPC_GAP_SECONDS = 1.2  # Studionet allows 60 requests per minute per IP


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------- results


@dataclass
class TxRecord:
    label: str
    account: str
    function: str
    tx_hash: str = ""
    status: str = ""
    result: str = ""
    seconds: float = 0.0
    expected: str = "success"
    note: str = ""

    @property
    def link(self) -> str:
        return EXPLORER_TX + self.tx_hash if self.tx_hash else ""

    @property
    def succeeded(self) -> bool | None:
        text = self.result.upper()
        if "FINISHED_WITH_RETURN" in text:
            return True
        if "FINISHED_WITH_ERROR" in text or "ERROR" in text:
            return False
        return None


@dataclass
class Check:
    name: str
    passed: bool
    expected: str
    observed: str
    section: str = ""


@dataclass
class Report:
    started: str = field(default_factory=utc_now)
    finished: str = ""
    environment: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    txs: list = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    fatal: str = ""

    def check(self, section: str, name: str, passed: bool, expected: Any, observed: Any) -> bool:
        record = Check(name, bool(passed), str(expected), str(observed), section)
        self.checks.append(record)
        log(("PASS " if passed else "FAIL ") + f"{section}: {name} (expected {expected}, saw {observed})")
        return bool(passed)

    @property
    def all_passed(self) -> bool:
        return not self.fatal and bool(self.checks) and all(c.passed for c in self.checks)


# -------------------------------------------------------------------------- chain access


class ChainError(RuntimeError):
    pass


class Chain:
    """What the scenario needs from a network. Real and simulated chains implement it."""

    def now(self) -> int: ...
    def sleep_until(self, timestamp: int, reason: str) -> None: ...
    def address(self, name: str) -> str: ...
    def write(self, name: str, function: str, args: list, value: int = 0,
              label: str = "", expect: str = "success") -> TxRecord: ...
    def read(self, function: str, args: list) -> Any: ...
    def balance(self, name_or_address: str) -> int: ...
    def fund(self, name: str, wei: int) -> bool: ...


class GenLayerChain(Chain):
    """Studionet through genlayer-py. Only thin, defensive wrappers live here."""

    def __init__(self, contract_address: str, state: dict, rpc_url: str | None, wait_seconds: int):
        try:
            from eth_account import Account
            from genlayer_py import create_account, create_client
            from genlayer_py.chains import studionet
        except ImportError as error:  # pragma: no cover - environment dependent
            raise ChainError(
                "genlayer-py is not installed. Run: python -m pip install -r requirements.txt"
            ) from error
        self._Account = Account
        self._create_account = create_account
        self.contract = contract_address
        self.state = state
        self.rpc_url = rpc_url or self._chain_rpc(studionet)
        self.wait_seconds = wait_seconds
        self._last_rpc = 0.0
        self.accounts: dict[str, Any] = {}
        for name in ["owner", "holder", "stranger"]:
            key = state["keys"].get(name)
            if key is None:
                account = create_account()
                state["keys"][name] = self._key_hex(account)
            else:
                account = Account.from_key(key)
            self.accounts[name] = account
        self.client = create_client(chain=studionet, account=self.accounts["owner"])

    @staticmethod
    def _chain_rpc(chain) -> str:
        try:
            return chain.rpc_urls["default"]["http"][0]
        except Exception:  # noqa: BLE001
            return DEFAULT_RPC

    @staticmethod
    def _key_hex(account) -> str:
        key = getattr(account, "key", None) or getattr(account, "_private_key")
        text = key.hex() if hasattr(key, "hex") else str(key)
        return text if text.startswith("0x") else "0x" + text

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_rpc
        if gap < MIN_RPC_GAP_SECONDS:
            time.sleep(MIN_RPC_GAP_SECONDS - gap)
        self._last_rpc = time.monotonic()

    def _rpc(self, method: str, params: list) -> Any:
        self._throttle()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            self.rpc_url, data=body,
            headers={"content-type": "application/json", "accept": "application/json",
                     "user-agent": "handback-live-e2e/0.1 (+python-urllib)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise ChainError(f"{method}: HTTP {error.code} from {self.rpc_url}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise ChainError(f"{method}: cannot reach {self.rpc_url}: {error}") from error
        if "error" in payload:
            raise ChainError(f"{method}: {payload['error']}")
        return payload.get("result")

    def now(self) -> int:
        return int(time.time())

    def sleep_until(self, timestamp: int, reason: str) -> None:
        remaining = timestamp - self.now()
        if remaining > 0:
            log(f"waiting {remaining}s for {reason}")
        while True:
            remaining = timestamp - self.now()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 30))

    def address(self, name: str) -> str:
        return self.accounts[name].address

    def balance(self, name_or_address: str) -> int:
        address = self.address(name_or_address) if name_or_address in self.accounts else name_or_address
        return int(self._rpc("eth_getBalance", [address, "latest"]), 16)

    def fund(self, name: str, wei: int) -> bool:
        address = self.address(name)
        for params in ([address, wei], [address, hex(wei)]):
            try:
                self._rpc("sim_fundAccount", params)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # ---- contract calls
    def _wait(self, tx_hash: str):
        interval_ms = 4000
        retries = max(10, int(self.wait_seconds * 1000 / interval_ms))
        try:
            return self.client.wait_for_transaction_receipt(
                transaction_hash=tx_hash, wait_until="finalized", interval=interval_ms,
                retries=retries,
            )
        except TypeError:
            from genlayer_py.types import TransactionStatus
            return self.client.wait_for_transaction_receipt(
                transaction_hash=tx_hash, status=TransactionStatus.FINALIZED,
                interval=interval_ms, retries=retries,
            )

    @staticmethod
    def _get(node: Any, key: str) -> Any:
        return node.get(key) if isinstance(node, dict) else getattr(node, key, None)

    @classmethod
    def _execution_outcome(cls, receipt: Any) -> tuple[str, str]:
        """(FINISHED_WITH_RETURN | FINISHED_WITH_ERROR | "", raw evidence).

        Only the final leader receipt and the top-level execution result are consulted.
        Validator receipts and nested history can contain errors that consensus overruled,
        and the consensus vote (for example MAJORITY_AGREE) never says whether a call reverted.
        """
        leader_text = ""
        consensus = cls._get(receipt, "consensus_data")
        leaders = cls._get(consensus, "leader_receipt") if consensus is not None else None
        if isinstance(leaders, (list, tuple)) and leaders:
            leader_text = str(cls._get(leaders[0], "execution_result") or "").upper()
        top = cls._get(receipt, "tx_execution_result_name")
        top_text = str(getattr(top, "name", top) or "").upper()
        evidence = f"leader={leader_text or '-'}, top={top_text or '-'}"
        if leader_text in ("SUCCESS", "ERROR"):
            return ("FINISHED_WITH_RETURN" if leader_text == "SUCCESS" else "FINISHED_WITH_ERROR"), evidence
        if top_text in ("FINISHED_WITH_RETURN", "FINISHED_WITH_ERROR"):
            return top_text, evidence
        return "", evidence

    def _dump_receipt(self, tx_hash: str, receipt: Any) -> None:
        try:
            folder = STATE_DIR / "receipts"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{tx_hash}.json").write_text(json.dumps(receipt, indent=2, default=str)[:60000])
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _transient(error: Exception) -> bool:
        text = str(error)
        return any(marker in text for marker in ("502", "503", "504", "Bad gateway", "invalid JSON",
                                                 "timed out", "Connection"))

    @staticmethod
    def _field(receipt: Any, *names: str) -> str:
        for name in names:
            value = receipt.get(name) if isinstance(receipt, dict) else getattr(receipt, name, None)
            if value not in (None, ""):
                return str(getattr(value, "name", value))
        return ""

    def write(self, name: str, function: str, args: list, value: int = 0,
              label: str = "", expect: str = "success") -> TxRecord:
        record = TxRecord(label or function, self.address(name), function, expected=expect)
        started = time.monotonic()
        try:
            tx_hash = ""
            for attempt in range(4):
                self._throttle()
                try:
                    tx_hash = self.client.write_contract(
                        address=self.contract, function_name=function, account=self.accounts[name],
                        args=args, value=value,
                    )
                    break
                except Exception as error:  # noqa: BLE001
                    if attempt == 3 or not self._transient(error):
                        raise
                    time.sleep(5 * (attempt + 1))
            record.tx_hash = str(tx_hash)
            receipt = self._wait(record.tx_hash)
            self._dump_receipt(record.tx_hash, receipt)
            record.status = self._field(receipt, "status_name", "statusName", "status")
            record.result, evidence = self._execution_outcome(receipt)
            vote = self._field(receipt, "result_name", "resultName")
            record.note = f"consensus={vote or '?'}; {evidence}"
        except Exception as error:  # noqa: BLE001
            record.note = f"{type(error).__name__}: {error}"
            if expect == "success":
                record.result = record.result or "SUBMISSION_ERROR"
        record.seconds = round(time.monotonic() - started, 1)
        return record

    def read(self, function: str, args: list) -> Any:
        for attempt in range(4):
            self._throttle()
            try:
                return self.client.read_contract(address=self.contract, function_name=function,
                                                 args=args)
            except Exception as error:  # noqa: BLE001
                if attempt == 3 or not self._transient(error):
                    raise
                time.sleep(5 * (attempt + 1))

    # ---- deployment (best effort convenience)
    def deploy(self, code: str) -> str:
        self._throttle()
        tx_hash = str(self.client.deploy_contract(code=code, args=[], account=self.accounts["owner"]))
        receipt = self._wait(tx_hash)
        found = _find_address(receipt)
        if not found:
            raise ChainError(
                f"Deployment {tx_hash} finalized but the contract address could not be read "
                "from the receipt. Copy it from the Studionet explorer and pass --address."
            )
        return found


def _find_address(node: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    keys = ("contract_address", "contractAddress", "created_address", "recipient", "to_address")
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.startswith("0x") and len(value) == 42 \
                    and int(value, 16) != 0:
                return value
        for value in node.values():
            found = _find_address(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_address(value, depth + 1)
            if found:
                return found
    return ""


# --------------------------------------------------------------------------- fixtures


def git_output(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return ""


def fixture_base_url(override: str | None) -> str:
    if override:
        return override.rstrip("/") + "/"
    repository = os.environ.get("GITHUB_REPOSITORY") or _repo_from_remote()
    commit = git_output("rev-parse", "HEAD")
    if not repository or not commit:
        raise ChainError(
            "Cannot derive the fixture URL. Set --fixture-base-url to a public, commit-pinned "
            "directory URL that serves the files in fixtures/live/."
        )
    return f"https://raw.githubusercontent.com/{repository}/{commit}/fixtures/live/"


def _repo_from_remote() -> str:
    remote = git_output("config", "--get", "remote.origin.url")
    for prefix in ("https://github.com/", "git@github.com:"):
        if remote.startswith(prefix):
            return remote[len(prefix):].removesuffix(".git")
    return ""


def load_fixtures(base_url: str, verify: bool) -> dict[str, dict]:
    names = {
        "inventory": "inventory.md",
        "a_owner": "a_owner.md", "a_holder": "a_holder.md",
        "b_owner": "b_owner.md",
        "c_owner_promised": "c_owner_promised.md", "c_owner_served": "c_owner_served.md",
        "f_owner": "f_owner.md",
        "holder_ok": "holder_ok.md",
    }
    fixtures = {}
    for key, filename in names.items():
        raw = (FIXTURE_DIR / filename).read_bytes()
        fixtures[key] = {"file": filename, "url": base_url + filename, "bytes": raw,
                         "hash": sha256_hex(raw)}
    if verify:
        problems = []
        for key, item in fixtures.items():
            try:
                with urllib.request.urlopen(item["url"], timeout=30) as response:
                    served = response.read()
            except (urllib.error.URLError, TimeoutError) as error:
                problems.append(f"{item['url']} -> {error}")
                continue
            if served != item["bytes"]:
                problems.append(f"{item['url']} serves different bytes than the local file")
        if problems:
            raise ChainError(
                "Fixture check failed. Commit and push fixtures/live/ to a PUBLIC repository "
                "first (validators must be able to fetch them):\n  " + "\n  ".join(problems)
            )
    return fixtures


# ---------------------------------------------------------------------------- scenario

# What each loan is meant to demonstrate and what the contract must end up doing with it.
PLAN = {
    "A": dict(title="Photo kit, weekend shoot", claim=[2, 3, 4, 5], accept=[5],
              owner_doc="a_owner", holder_doc="a_holder", hand_back=True,
              claim_window=900, respond_window=900,
              ruling="ruled", deducted=[2, 5], settlement="review_ruled", forfeited=False),
    "B": dict(title="Photo kit, studio week", claim=[1, 2], accept=[],
              owner_doc="b_owner", holder_doc="holder_ok", hand_back=True,
              claim_window=900, respond_window=900,
              ruling="owner_foul", deducted=[], settlement="review_owner_foul", forfeited=False),
    "C": dict(title="Photo kit, wedding", claim=[1, 2], accept=[],
              owner_doc="c_owner_served", owner_commit_doc="c_owner_promised",
              holder_doc="holder_ok", hand_back=True, claim_window=900, respond_window=900,
              ruling="owner_evidence_tampered", deducted=[],
              settlement="review_owner_evidence_tampered", forfeited=True),
    "D": dict(title="Photo kit, quiet fortnight", claim=None, accept=None,
              owner_doc=None, holder_doc="holder_ok", hand_back=True,
              claim_window=240, respond_window=900,
              ruling="", deducted=[], settlement="no_claim_deposit_returned", forfeited=False),
    "E": dict(title="Photo kit, one afternoon", claim=None, accept=None,
              owner_doc=None, holder_doc=None, hand_back=True,
              claim_window=900, respond_window=900,
              ruling="", deducted=[], settlement="released_by_owner", forfeited=False),
    "F": dict(title="Photo kit, silent holder", claim=[3], accept=None,
              owner_doc="f_owner", holder_doc=None, hand_back=False,
              claim_window=900, respond_window=240,
              ruling="ruled", deducted=[3], settlement="review_ruled", forfeited=False),
}
REVIEWED = ["A", "B", "C"]


def expected_amounts(letter: str) -> tuple[int, int, int]:
    """(owner credit, holder credit, deducted) this loan must produce once settled."""
    plan = PLAN[letter]
    deducted = sum(CAPS[item - 1] for item in plan["deducted"])
    owner = deducted + (0 if plan["forfeited"] else STAKE)
    holder = DEPOSIT - deducted + (STAKE if plan["forfeited"] else 0)
    return owner, holder, deducted


class Scenario:
    def __init__(self, chain: Chain, report: Report, fixtures: dict, state: dict,
                 accept_seconds: int, return_seconds: int, state_path: Path | None):
        self.chain = chain
        self.report = report
        self.fixtures = fixtures
        self.state = state
        self.accept_seconds = accept_seconds
        self.return_seconds = return_seconds
        self.state_path = state_path
        self.owner = chain.address("owner")
        self.holder = chain.address("holder")
        self.ids = state["loan_ids"]

    # ---- helpers
    def save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=2))

    def tx(self, name: str, function: str, args: list, *, value: int = 0, label: str = "",
           expect: str = "success", section: str = "") -> TxRecord:
        record = self.chain.write(name, function, args, value=value, label=label or function,
                                  expect=expect)
        self.report.txs.append(record)
        outcome = record.succeeded
        if expect == "success":
            log(f"tx {record.label}: {record.result or 'UNKNOWN'} [{record.note}] "
                f"({record.seconds}s)")
            if outcome is False or record.result == "SUBMISSION_ERROR":
                self.report.check(section or "transactions", f"{record.label} executed", False,
                                  "FINISHED_WITH_RETURN", record.result or record.note)
        else:
            log(f"tx {record.label}: expected revert, saw {record.result or 'UNKNOWN'} [{record.note}]")
        return record

    def view(self, function: str, args: list | None = None):
        return self.chain.read(function, args or [])

    def loan(self, letter: str) -> dict:
        return self.view("get_loan", [self.owner, self.ids[letter]])

    def claim(self, letter: str) -> dict:
        return self.view("get_claim", [self.owner, self.ids[letter]])

    def credit(self, name: str) -> int:
        return int(self.view("get_available_credit", [self.chain.address(name)]))

    def ref(self, letter: str) -> list:
        return [self.owner, self.ids[letter]]

    def doc(self, key: str) -> dict:
        return self.fixtures[key]

    def owner_report_for(self, letter: str) -> tuple[str, str]:
        """URL the validators will fetch and the SHA-256 the owner committed to."""
        plan = PLAN[letter]
        served = self.doc(plan["owner_doc"])
        committed = self.doc(plan.get("owner_commit_doc", plan["owner_doc"]))
        return served["url"], committed["hash"]

    def revert_check(self, section: str, name: str, who: str, function: str, args: list,
                     value: int = 0) -> None:
        """A call that must not change anything. State is compared before and after."""
        before = json.dumps(self.view("get_accounting"), sort_keys=True)
        record = self.tx(who, function, args, value=value, label=name, expect="revert",
                         section=section)
        after = json.dumps(self.view("get_accounting"), sort_keys=True)
        rejected = record.succeeded is False or record.result == "SUBMISSION_ERROR"
        self.report.check(section, f"{name}: rejected", rejected,
                          "FINISHED_WITH_ERROR (or refused at submission)",
                          record.result or f"UNKNOWN ({record.note})")
        self.report.check(section, f"{name}: books unchanged", before == after, "unchanged",
                          "unchanged" if before == after else "CHANGED")

    def require_time_before(self, key: str, margin: int, doing: str) -> None:
        """Stop with a clear message instead of racing a deadline that is about to pass."""
        deadline = int(self.state.get(key, 0))
        if deadline and self.chain.now() > deadline - margin:
            raise ChainError(
                f"The {key} deadline of this run is too close or already past while {doing}. "
                "Do not --resume an old run: start a fresh one (omit --resume) and consider "
                "raising --accept-seconds / --return-seconds."
            )

    def baseline(self) -> dict:
        """Contract-wide accounting before this run touched anything (other runs share it)."""
        if "baseline" not in self.state:
            accounting = self.view("get_accounting")
            self.state["baseline"] = {k: int(accounting[k]) for k in (
                "total_credits", "total_locked_deposits", "total_locked_stakes",
                "accounted_liabilities")}
            self.state["credit_before"] = {"owner": self.credit("owner"),
                                           "holder": self.credit("holder")}
            self.save()
        return self.state["baseline"]

    def delta(self, accounting: dict, key: str) -> int:
        return int(accounting[key]) - int(self.state.get("baseline", {}).get(key, 0))

    # ---- phases
    def funding_needs(self) -> dict:
        n = len(LOANS)
        return {"owner": n * STAKE + 2 * 10**14, "holder": n * DEPOSIT + 2 * 10**14,
                "stranger": 10**14}

    def print_funding_plan(self) -> None:
        log("test accounts (kept in .live/accounts.json, which is git-ignored):")
        for name, wei in self.funding_needs().items():
            log(f"  {name:8s} {self.chain.address(name)}  needs {wei / GEN:.5f} GEN")

    def phase_funding(self) -> None:
        section = "1. Funding"
        self.baseline()
        self.print_funding_plan()
        needs = self.funding_needs()
        for name, wei in needs.items():
            if self.chain.balance(name) >= wei:
                continue
            self.chain.fund(name, wei * 2)
        shortfalls = {n: w for n, w in needs.items() if self.chain.balance(n) < w}
        for name, wei in needs.items():
            have = self.chain.balance(name)
            self.report.check(section, f"{name} holds enough GEN ({wei / GEN:.5f})",
                              have >= wei, f">= {wei}", have)
        if shortfalls:
            lines = "\n".join(f"  {self.chain.address(n)}  needs {w / GEN:.5f} GEN"
                              for n, w in shortfalls.items())
            raise ChainError(
                "Automatic funding is not available on this network or was refused. Send GEN to:\n"
                f"{lines}\nthen re-run with --resume {self.state['run_id']}"
            )

    def phase_open(self) -> None:
        section = "2. Offers"
        if self.state.get("opened"):
            return
        inv = self.doc("inventory")
        self.tx("owner", "deposit_credit", [], value=len(LOANS) * STAKE,
                label="owner deposits one stake per offer", section=section)
        # The stake is in credit, so only the self-lending rule can reject this call.
        self.revert_check(section, "an owner cannot lend to itself", "owner", "open_loan", [
            "SELF-CHECK", self.owner, "Self loan", "Must be rejected.", inv["url"], inv["hash"],
            5, caps_text(CAPS), DEPOSIT, STAKE, self.chain.now() + 900, self.chain.now() + 1800,
            240, 240])
        now = self.chain.now()
        self.state["accept_by"] = now + self.accept_seconds
        self.state["return_by"] = self.state["accept_by"] + self.return_seconds
        self.state["claim_cutoff"] = self.state["return_by"] + 900
        self.save()
        for letter in LOANS:
            plan = PLAN[letter]
            self.require_time_before("accept_by", 240, "opening offers")
            self.tx("owner", "open_loan", [
                self.ids[letter], self.holder, plan["title"],
                "Camera kit lent against an itemized deposit; each numbered item has a fixed "
                "deduction cap agreed in advance.",
                inv["url"], inv["hash"], len(CAPS), caps_text(CAPS), DEPOSIT, STAKE,
                self.state["accept_by"], self.state["return_by"], plan["claim_window"],
                plan["respond_window"],
            ], label=f"open loan {letter}", section=section)
        self.state["opened"] = True
        self.save()
        for letter in LOANS:
            loan = self.loan(letter)
            self.report.check(section, f"loan {letter} is offered with the committed terms",
                              loan["status"] == "offered" and loan["caps"] == CAPS
                              and loan["deposit_wei"] == DEPOSIT and loan["stake_wei"] == STAKE
                              and loan["inventory_hash"] == inv["hash"],
                              "offered, caps/deposit/stake/hash as sent", loan["status"])
        accounting = self.view("get_accounting")
        locked = self.delta(accounting, "total_locked_stakes")
        self.report.check(section, "one stake locked per offer (change since run start)",
                          locked == len(LOANS) * STAKE, len(LOANS) * STAKE, locked)
        self.report.check(section, "owner credit fully allocated",
                          self.credit("owner") == self.state["credit_before"]["owner"],
                          self.state["credit_before"]["owner"], self.credit("owner"))

    def phase_accept(self) -> None:
        section = "3. Acceptance"
        if self.state.get("accepted"):
            return
        inv = self.doc("inventory")
        self.revert_check(section, "a stranger cannot accept someone else's offer", "stranger",
                          "accept_loan", [self.owner, self.ids["A"], inv["hash"]])
        self.tx("holder", "deposit_credit", [], value=len(LOANS) * DEPOSIT,
                label="holder deposits one deposit per loan", section=section)
        self.revert_check(section, "accepting with the wrong inventory hash", "holder",
                          "accept_loan", [self.owner, self.ids["A"], sha256_hex(b"not the inventory")])
        for letter in LOANS:
            self.require_time_before("accept_by", 45, "accepting offers")
            self.tx("holder", "accept_loan", [self.owner, self.ids[letter], inv["hash"]],
                    label=f"holder accepts loan {letter}", section=section)
        self.state["accepted"] = True
        self.save()
        for letter in LOANS:
            loan = self.loan(letter)
            self.report.check(section, f"loan {letter} is active", loan["status"] == "active",
                              "active", loan["status"])
        accounting = self.view("get_accounting")
        deposits = self.delta(accounting, "total_locked_deposits")
        self.report.check(section, "one deposit locked per loan (change since run start)",
                          deposits == len(LOANS) * DEPOSIT, len(LOANS) * DEPOSIT, deposits)
        self.revert_check(section, "accepting an already active loan", "holder", "accept_loan",
                          [self.owner, self.ids["A"], inv["hash"]])

    def phase_return(self) -> None:
        section = "4. Return and release"
        if self.state.get("returned"):
            return
        self.revert_check(section, "the owner cannot hand back on the holder's behalf", "owner",
                          "hand_back", [self.owner, self.ids["A"], "", ""])
        self.revert_check(section, "a report needs both a URL and a hash", "holder", "hand_back",
                          [self.owner, self.ids["A"], self.doc("a_holder")["url"], ""])
        for letter in LOANS:
            plan = PLAN[letter]
            if not plan["hand_back"]:
                continue
            report_url = report_hash = ""
            if plan["holder_doc"]:
                report_url = self.doc(plan["holder_doc"])["url"]
                report_hash = self.doc(plan["holder_doc"])["hash"]
            self.tx("holder", "hand_back", [self.owner, self.ids[letter], report_url, report_hash],
                    label=f"holder hands back loan {letter}", section=section)
        self.revert_check(section, "handing back twice", "holder", "hand_back",
                          [self.owner, self.ids["A"], "", ""])
        self.revert_check(section, "a stranger cannot release a deposit", "stranger",
                          "release_loan", [self.ids["E"]])
        self.revert_check(section, "the holder cannot release its own deposit", "holder",
                          "release_loan", [self.ids["E"]])
        self.tx("owner", "release_loan", [self.ids["E"]], label="owner releases loan E early",
                section=section)
        self.state["returned"] = True
        self.save()
        for letter in LOANS:
            loan = self.loan(letter)
            if PLAN[letter]["hand_back"]:
                self.report.check(section, f"loan {letter} records the return",
                                  loan["returned_at"] > 0, "returned_at > 0", loan["returned_at"])
        for letter in ("A", "B", "C", "D"):
            want = self.doc(PLAN[letter]["holder_doc"])
            loan = self.loan(letter)
            self.report.check(section, f"loan {letter} holder report is committed at hand-back",
                              loan["holder_report_url"] == want["url"]
                              and loan["holder_report_hash"] == want["hash"],
                              want["hash"], loan["holder_report_hash"])
        loan = self.loan("E")
        self.report.check(section, "loan E settled by owner release",
                          loan["status"] == "settled" and loan["settlement"] == "released_by_owner"
                          and loan["deducted_wei"] == 0,
                          "settled / released_by_owner / 0 deducted",
                          f"{loan['status']} / {loan['settlement']} / {loan['deducted_wei']}")

    def phase_claims(self) -> None:
        section = "5. Claims"
        if self.state.get("claimed"):
            return
        url_f, hash_f = self.owner_report_for("F")
        if self.chain.now() < self.state["return_by"] - 30:  # never risk a real claim
            self.revert_check(section, "claiming before the due date without a return", "owner",
                              "file_claim", [self.ids["F"], mask_of([3]), url_f, hash_f])
        url_a, hash_a = self.owner_report_for("A")
        self.revert_check(section, "the holder cannot file a claim", "holder", "file_claim",
                          [self.ids["A"], mask_of([2]), url_a, hash_a])
        self.revert_check(section, "claiming an item that does not exist", "owner", "file_claim",
                          [self.ids["A"], mask_of([6]), url_a, hash_a])
        self.revert_check(section, "an empty claim", "owner", "file_claim",
                          [self.ids["A"], 0, url_a, hash_a])
        for letter in REVIEWED:
            self.require_time_before("claim_cutoff", 60, "filing claims")
            url, committed = self.owner_report_for(letter)
            self.tx("owner", "file_claim", [self.ids[letter], mask_of(PLAN[letter]["claim"]),
                                            url, committed],
                    label=f"owner claims on loan {letter}", section=section)
        self.state["claimed"] = True
        self.save()
        for letter in REVIEWED:
            loan, claim = self.loan(letter), self.claim(letter)
            self.report.check(section, f"loan {letter} claim recorded",
                              loan["status"] == "claimed"
                              and claim["claimed_items"] == sorted(PLAN[letter]["claim"]),
                              f"claimed {sorted(PLAN[letter]['claim'])}",
                              f"{loan['status']} {claim['claimed_items']}")
        self.revert_check(section, "the owner cannot claim twice", "owner", "file_claim",
                          [self.ids["A"], mask_of([1]), url_a, hash_a])
        self.revert_check(section, "the holder cannot attach a second report", "holder",
                          "hand_back", [self.owner, self.ids["A"], "", ""])

    def phase_respond(self) -> None:
        section = "6. Responses"
        if self.state.get("responded"):
            return
        self.revert_check(section, "the owner cannot respond for the holder", "owner",
                          "respond_to_claim", [self.owner, self.ids["A"], mask_of([5]), "", ""])
        self.revert_check(section, "accepting an item that was never claimed", "holder",
                          "respond_to_claim", [self.owner, self.ids["A"], mask_of([1]), "", ""])
        for letter in REVIEWED:
            self.require_time_before("claim_cutoff", 60, "responding")
            self.tx("holder", "respond_to_claim", [
                self.owner, self.ids[letter], mask_of(PLAN[letter]["accept"]), "", ""],
                label=f"holder responds on loan {letter}", section=section)
        self.state["responded"] = True
        self.save()
        for letter in REVIEWED:
            claim = self.claim(letter)
            disputed = sorted(set(PLAN[letter]["claim"]) - set(PLAN[letter]["accept"]))
            self.report.check(section, f"loan {letter} disputed items",
                              claim["disputed_items"] == disputed, disputed,
                              claim["disputed_items"])
        self.revert_check(section, "responding twice", "holder", "respond_to_claim",
                          [self.owner, self.ids["A"], 0, "", ""])
        self.revert_check(section, "a stranger cannot request a review", "stranger",
                          "review_claim", [self.owner, self.ids["A"]])

    def review_until_ruled(self, letter: str, section: str, max_tries: int = 5) -> dict:
        callers = ["owner", "holder"]
        for attempt in range(max_tries):
            claim = self.claim(letter)
            if claim["ruling"] or claim["review_calls"] >= 8 or claim["attempts"] >= 3:
                break
            if claim["retry_at"]:
                self.chain.sleep_until(claim["retry_at"] + 2, f"loan {letter} review cooldown")
            self.tx(callers[attempt % 2], "review_claim", self.ref(letter),
                    label=f"review loan {letter} (try {attempt + 1})", section=section)
        return self.claim(letter)

    def check_ruling(self, section: str, letter: str, claim: dict) -> None:
        plan = PLAN[letter]
        self.report.check(
            section, f"loan {letter} ruled '{plan['ruling']}'", claim["ruling"] == plan["ruling"],
            plan["ruling"],
            f"{claim['ruling'] or '-'} (confidence {claim['confidence']}, "
            f"owner_attempt={claim['owner_instruction_attempt'] or '-'}, "
            f"holder_attempt={claim['holder_instruction_attempt'] or '-'}, "
            f"reason={claim['reason'] or '-'})")

    def phase_review(self) -> None:
        section = "7. Validator review"
        if self.state.get("reviewed"):
            return
        details = {}
        for letter in REVIEWED:
            self.require_time_before("claim_cutoff", 0, "reviewing")
            details[letter] = self.review_until_ruled(letter, section)
            self.check_ruling(section, letter, details[letter])
        a = details["A"]
        self.report.check(section, "loan A: only item I2 is ruled damaged (I3 pre-existing, "
                          "I4 ordinary wear)", a["ruled_mask"] == mask_of([2]), mask_of([2]),
                          a["ruled_mask"])
        self.report.check(section, "loan C: the mismatch is blamed on the owner's report",
                          details["C"]["reason"] == "owner:hash_mismatch", "owner:hash_mismatch",
                          details["C"]["reason"])
        self.revert_check(section, "a decisive ruling cannot be re-rolled", "owner",
                          "review_claim", self.ref("A"))
        self.revert_check(section, "the owner cannot withdraw a ruled claim", "owner",
                          "withdraw_claim", [self.ids["A"]])
        self.report.sections[section] = {"claims": details}
        self.state["reviewed"] = True
        self.save()

    def check_settled(self, section: str, letter: str) -> None:
        plan = PLAN[letter]
        owner_wei, holder_wei, deducted = expected_amounts(letter)
        loan = self.loan(letter)
        self.report.check(section, f"loan {letter} settled as '{plan['settlement']}'",
                          loan["status"] == "settled" and loan["settlement"] == plan["settlement"],
                          f"settled / {plan['settlement']}",
                          f"{loan['status']} / {loan['settlement']}")
        self.report.check(section, f"loan {letter} deducted exactly the agreed caps",
                          loan["deducted_wei"] == deducted
                          and loan["deducted_mask"] == mask_of(plan["deducted"]),
                          f"{deducted} wei, items {plan['deducted']}",
                          f"{loan['deducted_wei']} wei, items {items_of(loan['deducted_mask'])}")
        self.report.check(section, f"loan {letter} returned the rest of the deposit",
                          loan["returned_wei"] == DEPOSIT - deducted, DEPOSIT - deducted,
                          loan["returned_wei"])
        want_forfeit = STAKE if plan["forfeited"] else 0
        self.report.check(section, f"loan {letter} stake forfeited: {bool(want_forfeit)}",
                          loan["stake_forfeited_wei"] == want_forfeit, want_forfeit,
                          loan["stake_forfeited_wei"])

    def phase_settle_reviewed(self) -> None:
        section = "8. Settlement of reviewed loans"
        if self.state.get("settled_reviewed"):
            return
        for letter in REVIEWED:
            self.tx("stranger", "settle_loan", self.ref(letter),
                    label=f"stranger settles loan {letter} (permissionless)", section=section)
        self.state["settled_reviewed"] = True
        self.save()
        for letter in REVIEWED:
            self.check_settled(section, letter)
        self.revert_check(section, "settling a second time", "stranger", "settle_loan",
                          self.ref("A"))
        self.revert_check(section, "a settled loan cannot be reviewed again", "owner",
                          "review_claim", self.ref("B"))

    def phase_silent_holder_and_no_claim(self) -> None:
        section = "9. Silent holder and unclaimed loan"
        if self.state.get("tail_done"):
            return
        # F: the holder never responds. The owner may claim only once the loan is due.
        self.chain.sleep_until(self.state["return_by"] + 3, "loan F to fall due")
        url_f, hash_f = self.owner_report_for("F")
        self.tx("owner", "file_claim", [self.ids["F"], mask_of(PLAN["F"]["claim"]), url_f, hash_f],
                label="owner claims on loan F after the due date", section=section)
        claim_f = self.claim("F")
        self.report.check(section, "loan F claim recorded with a response deadline",
                          claim_f["claimed_items"] == [3] and claim_f["respond_by"] > 0,
                          "items [3], respond_by set",
                          f"{claim_f['claimed_items']}, respond_by={claim_f['respond_by']}")
        self.revert_check(section, "reviewing before the response window ends", "owner",
                          "review_claim", self.ref("F"))
        # D: nobody claims; the deposit comes back when the claim window ends.
        claim_by_d = self.loan("D")["claim_by"]
        self.revert_check(section, "settling loan D while its claim window is open", "stranger",
                          "settle_loan", self.ref("D"))
        self.chain.sleep_until(claim_by_d + 3, "loan D's claim window to close")
        self.tx("stranger", "settle_loan", self.ref("D"),
                label="stranger settles unclaimed loan D", section=section)
        self.check_settled(section, "D")
        # F again: review on the owner's evidence alone.
        self.chain.sleep_until(claim_f["respond_by"] + 3, "loan F's response window to close")
        self.revert_check(section, "responding after the window", "holder", "respond_to_claim",
                          [self.owner, self.ids["F"], 0, "", ""])
        details_f = self.review_until_ruled("F", section)
        self.check_ruling(section, "F", details_f)
        self.report.check(section, "loan F: the unanswered claim is ruled on item I3 alone",
                          details_f["ruled_mask"] == mask_of([3]), mask_of([3]),
                          details_f["ruled_mask"])
        self.tx("stranger", "settle_loan", self.ref("F"),
                label="stranger settles loan F", section=section)
        self.check_settled(section, "F")
        self.report.sections[section] = {"claims": {"F": details_f}}
        self.state["tail_done"] = True
        self.save()

    def phase_conservation(self) -> None:
        section = "10. Conservation"
        want_owner = sum(expected_amounts(L)[0] for L in LOANS)
        want_holder = sum(expected_amounts(L)[1] for L in LOANS)
        before = self.state["credit_before"]
        got_owner, got_holder = self.credit("owner"), self.credit("holder")
        self.report.check(section, "owner ledger credit equals the sum of loan outcomes",
                          got_owner - before["owner"] == want_owner, want_owner,
                          got_owner - before["owner"])
        self.report.check(section, "holder ledger credit equals the sum of loan outcomes",
                          got_holder - before["holder"] == want_holder, want_holder,
                          got_holder - before["holder"])
        total_in = len(LOANS) * (STAKE + DEPOSIT)
        self.report.check(section, "all deposited value is accounted for between the two sides",
                          want_owner + want_holder == total_in, total_in, want_owner + want_holder)
        accounting = self.view("get_accounting")
        locked = (self.delta(accounting, "total_locked_deposits")
                  + self.delta(accounting, "total_locked_stakes"))
        self.report.check(section, "nothing from this run is still locked (change since run start)",
                          locked == 0, 0, locked)
        self.report.check(section, "contract balance covers every liability",
                          accounting["solvent"], True, accounting["solvent"])
        self.report.sections[section] = {"accounting": accounting}

    def phase_withdraw(self) -> None:
        section = "11. Native GEN withdrawals"
        withdrawn = self.state.setdefault("withdrawn", {})
        for name in ("owner", "holder"):
            if withdrawn.get(name):
                continue
            wei = self.credit(name)
            if wei == 0:
                continue
            before = self.chain.balance(name)
            self.tx(name, "withdraw_credit", [], label=f"{name} withdraws credit", section=section)
            deadline = time.time() + 240
            after = before
            while time.time() < deadline:
                after = self.chain.balance(name)
                if after - before >= wei:
                    break
                time.sleep(8)
            gained = after - before
            self.report.check(section, f"{name} received {wei} wei", gained == wei, wei, gained)
            self.report.check(section, f"{name} ledger credit cleared", self.credit(name) == 0, 0,
                              self.credit(name))
            withdrawn[name] = True
            self.save()
        self.revert_check(section, "a stranger has nothing to withdraw", "stranger",
                          "withdraw_credit", [])
        accounting = self.view("get_accounting")
        liabilities = self.delta(accounting, "accounted_liabilities")
        self.report.check(section, "no liabilities remain from this run (change since run start)",
                          liabilities == 0, 0, liabilities)
        self.report.sections[section] = {"accounting": accounting}

    def run(self) -> None:
        for phase in (self.phase_funding, self.phase_open, self.phase_accept, self.phase_return,
                      self.phase_claims, self.phase_respond, self.phase_review,
                      self.phase_settle_reviewed, self.phase_silent_holder_and_no_claim,
                      self.phase_conservation, self.phase_withdraw):
            phase()


# -------------------------------------------------------------------------------- report


def write_report(report: Report, directory: Path, contract: str, base_url: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = directory / f"live-e2e-{stamp}.json"
    md_path = directory / f"live-e2e-{stamp}.md"
    passed = sum(1 for c in report.checks if c.passed)
    failed = len(report.checks) - passed
    payload = {
        "started": report.started, "finished": report.finished, "contract": contract,
        "fixture_base_url": base_url, "environment": report.environment,
        "passed": passed, "failed": failed, "fatal": report.fatal,
        "checks": [c.__dict__ for c in report.checks],
        "transactions": [dict(t.__dict__, link=t.link) for t in report.txs],
        "sections": report.sections,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    verdict = "PASS" if report.all_passed else "FAIL"
    lines = [
        f"# Handback live end-to-end report - {verdict}", "",
        "- Network: GenLayer Studionet",
        f"- Contract: [{contract}]({EXPLORER_ADDRESS}{contract})",
        f"- Started / finished (UTC): {report.started} / {report.finished}",
        f"- Checks: **{passed} passed, {failed} failed**",
        f"- Fixture base URL: `{base_url}`",
    ]
    for key, value in report.environment.items():
        lines.append(f"- {key}: `{value}`")
    if report.fatal:
        lines += ["", "## Run aborted", "", "```", report.fatal, "```"]
    lines += ["", "## Checks", "", "| Result | Section | Check | Expected | Observed |",
              "| --- | --- | --- | --- | --- |"]
    for check in report.checks:
        mark = "PASS" if check.passed else "**FAIL**"
        row = [mark, check.section, check.name, check.expected, check.observed]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |")
    lines += ["", "## Transactions", "", "| # | Step | Account | Function | Result | Status | Seconds | Tx |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for number, tx in enumerate(report.txs, 1):
        link = f"[{tx.tx_hash[:12]}...]({tx.link})" if tx.tx_hash else (tx.note or "-")
        expected = " (expected revert)" if tx.expected == "revert" else ""
        lines.append(f"| {number} | {tx.label}{expected} | `{tx.account[:10]}...` | `{tx.function}` | "
                     f"{tx.result or '-'} | {tx.status or '-'} | {tx.seconds} | {link} |")
    findings = {}
    for section in report.sections.values():
        findings.update(section.get("claims", {}))
    if findings:
        lines += ["", "## Validator findings", "",
                  "| Loan | Ruling | Ruled items | Confidence | Owner steering | Holder steering | "
                  "Reason | Rationale |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for letter, claim in sorted(findings.items()):
            items = items_of(claim["ruled_mask"])
            lines.append(
                f"| {letter} | {claim['ruling'] or '-'} | {items or '-'} | {claim['confidence']} | "
                f"{claim['owner_instruction_attempt'] or '-'} | "
                f"{claim['holder_instruction_attempt'] or '-'} | {claim['reason'] or '-'} | "
                f"{str(claim['rationale']).replace('|', '/')} |")
    lines += ["", "## Notes", "",
              "- Pass/fail is decided from state read back from the contract, not from receipts.",
              "- `FINISHED_WITH_ERROR` on a step marked *expected revert* is the desired outcome.",
              "- Verdicts come from real validators and a real LLM; a failed ruling check can mean "
              "the model disagreed with the fixture, so read the rationale before suspecting the "
              "contract.",
              "- Native GEN withdrawals are verified by comparing wallet balances before and after, "
              "so this report records whether delivery really happened on this network."]
    md_path.write_text("\n".join(lines) + "\n")
    return md_path, json_path


# ---------------------------------------------------------------------------------- main


def environment_info(chain_name: str) -> dict:
    info = {"python": platform.python_version(), "platform": platform.platform(),
            "network": chain_name, "git_commit": git_output("rev-parse", "HEAD") or "unknown",
            "contract_sha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()}
    for package in ("genlayer-py", "genlayer-test"):
        try:
            from importlib.metadata import version
            info[package] = version(package)
        except Exception:  # noqa: BLE001
            info[package] = "not installed"
    return info


def load_state(path: Path, resume: str | None) -> tuple[dict, Path]:
    if resume:
        candidate = STATE_DIR / f"run-{resume}.json"
        if not candidate.exists():
            raise ChainError(f"No saved run {resume} in {STATE_DIR}")
        return json.loads(candidate.read_text()), candidate
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    state = {"run_id": run_id, "keys": {},
             "loan_ids": {letter: f"LIVE-{run_id}-{letter}" for letter in LOANS}}
    keys_file = STATE_DIR / "accounts.json"
    if keys_file.exists():
        state["keys"] = json.loads(keys_file.read_text())
    return state, STATE_DIR / f"run-{run_id}.json"


def main(argv: list[str] | None = None, chain_factory: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--address", default=os.environ.get("HANDBACK_ADDRESS"),
                        help="deployed Handback contract (or env HANDBACK_ADDRESS)")
    parser.add_argument("--deploy", action="store_true", help="deploy contracts/handback.py first")
    parser.add_argument("--fixture-base-url", default=os.environ.get("FIXTURE_BASE_URL"))
    parser.add_argument("--skip-fixture-check", action="store_true")
    parser.add_argument("--accept-seconds", type=int, default=1500,
                        help="how long holders have to accept the offers (default 1500)")
    parser.add_argument("--return-seconds", type=int, default=600,
                        help="gap between the acceptance deadline and the due date (default 600)")
    parser.add_argument("--wait-seconds", type=int, default=900, help="per-transaction wait budget")
    parser.add_argument("--rpc-url", default=os.environ.get("GENLAYER_RPC_URL"))
    parser.add_argument("--resume", help="run id from a previous (interrupted) run")
    parser.add_argument("--check-only", action="store_true",
                        help="verify tooling, fixtures and RPC reachability, then stop")
    args = parser.parse_args(argv)

    report = Report()
    report.environment = environment_info("studionet")
    contract = args.address or ""
    base_url = ""
    exit_code = 1
    try:
        state, state_path = load_state(STATE_DIR / "accounts.json", args.resume)
        base_url = args.fixture_base_url and fixture_base_url(args.fixture_base_url) \
            or state.get("fixture_base_url") or fixture_base_url(None)
        state["fixture_base_url"] = base_url
        fixtures = load_fixtures(base_url, verify=not args.skip_fixture_check)
        log(f"fixtures verified at {base_url}")
        if not contract and not args.deploy and not chain_factory:
            raise ChainError("Pass --address of your deployed Handback contract, or use --deploy.")
        chain = (chain_factory(state) if chain_factory
                 else GenLayerChain(contract or "0x" + "0" * 40, state, args.rpc_url, args.wait_seconds))
        if args.deploy and not chain_factory:
            log("deploying contracts/handback.py")
            contract = chain.deploy(CONTRACT_PATH.read_text())
            chain.contract = contract
            log(f"deployed at {contract}")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "accounts.json").write_text(json.dumps(state["keys"], indent=2))
        info = chain.read("get_info", [])
        report.check("0. Preflight", "contract answers get_info as Handback",
                     info.get("name") == "Handback", "Handback", info.get("name"))
        report.environment["contract_version"] = info.get("version")
        scenario = Scenario(chain, report, fixtures, state, args.accept_seconds,
                            args.return_seconds, state_path)
        if args.check_only:
            scenario.print_funding_plan()
            log("check-only run finished: tooling, fixtures and contract are reachable")
            return 0 if report.all_passed else 1
        scenario.run()
        exit_code = 0 if report.all_passed else 1
    except ChainError as error:
        report.fatal = str(error)
        exit_code = 2
    except KeyboardInterrupt:
        report.fatal = "Interrupted. Resume with --resume " + (locals().get("state", {}) or {}).get("run_id", "")
        exit_code = 130
    except Exception:  # noqa: BLE001
        report.fatal = traceback.format_exc()
        exit_code = 2
    finally:
        report.finished = utc_now()
        if report.checks or report.fatal:
            md_path, json_path = write_report(report, REPORT_DIR, contract or "(none)", base_url)
            log(f"report written: {os.path.relpath(md_path)} and {json_path.name}")
        if report.fatal:
            log("ABORTED: " + report.fatal.strip().splitlines()[-1])
        passed = sum(1 for c in report.checks if c.passed)
        log(f"{passed}/{len(report.checks)} checks passed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
