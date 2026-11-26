"""Run the real live_e2e scenario against Direct Mode, with a simulated chain.

This cannot prove how real validators behave, but it proves the script drives the contract in
a coherent order, that every expected revert really reverts, and that its arithmetic matches
the contract's payouts. Web pages and model verdicts are mocked to match the fixtures.
"""

import importlib
import json

from tests.direct.support import NOW_TS, PROMPT_HEAD, analysis, at, deploy

live = importlib.import_module("scripts.live_e2e")
BASE = "https://fixtures.example.org/live/"


class SimChain(live.Chain):
    def __init__(self, vm, contract, accounts):
        self.vm = vm
        self.contract = contract
        self.keys = {"owner": accounts[0], "holder": accounts[1], "stranger": accounts[2]}
        self.clock = NOW_TS
        self.native = {name: 0 for name in self.keys}
        self.held = 0  # what the simulated contract holds in native GEN
        self.n = 0
        self.vm.warp(at(self.clock))

    def now(self):
        return self.clock

    def _tick(self, seconds=25):
        self.clock += seconds
        self.vm.warp(at(self.clock))

    def sleep_until(self, timestamp, reason):
        if timestamp > self.clock:
            self.clock = timestamp
            self.vm.warp(at(self.clock))

    def address(self, name):
        return "0x" + self.keys[name].hex()

    def fund(self, name, wei):
        self.native[name] += wei
        return True

    def balance(self, name_or_address):
        return self.native[name_or_address]

    def read(self, function, args):
        return getattr(self.contract, function)(*args)

    def write(self, name, function, args, value=0, label="", expect="success"):
        self._tick()
        self.n += 1
        record = live.TxRecord(label or function, self.address(name), function, expected=expect,
                               tx_hash=f"0x{self.n:064x}", status="FINALIZED")
        self.vm.sender = self.keys[name]
        self.vm.value = value
        credit_before = 0
        if function == "withdraw_credit":
            credit_before = self.contract.get_available_credit(self.address(name))
        try:
            getattr(self.contract, function)(*args)
            record.result = "FINISHED_WITH_RETURN"
            self.native[name] -= value
            self.native[name] += credit_before
            self.held += value - credit_before
            self.vm.deal(self.contract.address, self.held)
            assert self.native[name] >= 0, "simulated wallet overdrawn"
            assert self.held >= 0, "simulated contract overdrawn"
        except AssertionError:
            raise
        except Exception as error:  # noqa: BLE001
            record.result = "FINISHED_WITH_ERROR"
            record.note = str(error)[:160]
        finally:
            self.vm.value = 0
        return record


def _serve_fixtures(vm):
    for filename in ("inventory.md", "a_owner.md", "a_holder.md", "b_owner.md", "c_owner_promised.md",
                     "c_owner_served.md", "f_owner.md", "holder_ok.md"):
        vm.mock_web(BASE + filename, {"status": 200, "body": (live.FIXTURE_DIR / filename).read_bytes()})
    head = PROMPT_HEAD + r".*BEGIN_UNTRUSTED_OWNER_REPORT\n# Return inspection by the owner \(loan "
    vm.mock_llm(head + r"A\)", json.dumps(analysis(["damaged", "not_damaged", "not_damaged"])))
    vm.mock_llm(head + r"B\)", json.dumps(analysis(["damaged", "damaged"], owner="yes")))
    vm.mock_llm(head + r"F\)", json.dumps(analysis(["damaged"])))


def test_the_live_scenario_passes_against_the_contract_in_direct_mode(
    direct_vm, direct_deploy, direct_accounts, tmp_path, monkeypatch
):
    contract = deploy(direct_vm, direct_deploy)
    _serve_fixtures(direct_vm)
    monkeypatch.setattr(live, "STATE_DIR", tmp_path / ".live")
    monkeypatch.setattr(live, "REPORT_DIR", tmp_path / "reports")
    holder = {}

    def factory(state):
        holder["chain"] = SimChain(direct_vm, contract, direct_accounts)
        return holder["chain"]

    code = live.main(["--skip-fixture-check", "--fixture-base-url", BASE], chain_factory=factory)
    reports = sorted((tmp_path / "reports").glob("live-e2e-*.json"))
    assert reports, "the script must always write a report"
    payload = json.loads(reports[-1].read_text())
    failed = [c for c in payload["checks"] if not c["passed"]]
    assert not payload["fatal"], payload["fatal"]
    assert not failed, json.dumps(failed, indent=2)
    assert code == 0
    assert payload["passed"] >= 120
    # Every loan the scenario created ended up settled and every wallet was paid out.
    chain = holder["chain"]
    assert contract.get_accounting()["total_credits"] == 0
    assert chain.held == 0, "every wei deposited was withdrawn by exactly one side"
    total = len(live.LOANS) * (live.STAKE + live.DEPOSIT)
    assert chain.native["owner"] + chain.native["holder"] >= 0
    expected_owner = sum(live.expected_amounts(L)[0] for L in live.LOANS)
    expected_holder = sum(live.expected_amounts(L)[1] for L in live.LOANS)
    assert expected_owner + expected_holder == total
    markdown = next((tmp_path / "reports").glob("live-e2e-*.md")).read_text()
    assert "Handback live end-to-end report - PASS" in markdown
    assert "Validator findings" in markdown
