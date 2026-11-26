# Deployment and live verification (Studionet)

Handback targets GenLayer **Studionet**. The contract, tests and live script were developed against the real `genlayer-test` Direct Mode runner (GenVM `v0.2.16`) and `genvm-lint`. They were not run against the live network in the environment that produced them, so the first Codespace run below is the first contact with real validators, a real LLM and real native transfers. Treat it as the actual verification.

## 1. Prepare a Codespace

1. Push this repository to a **public** GitHub repository (validators must be able to fetch `fixtures/live/` over HTTPS) and open it in a Codespace; the dev container installs Python 3.12 and `requirements.txt`.
2. Run the full offline gate:

   ```bash
   python scripts/preflight.py --write-schema
   ```

   It checks source rules, runs `genvm-lint`, writes `artifacts/handback.abi.json`, and runs every Direct Mode test. Fix any failure before deploying; do not skip it. Commit the generated ABI.
3. Commit and push, so the commit-pinned fixture URLs exist.

## 2. Deploy

Deploy `contracts/handback.py` to Studionet with the GenLayer CLI or Studio (select the Studionet network), with no constructor arguments. Record the address.

```bash
python scripts/live_e2e.py --deploy        # optional: deploys through the runner, then runs
```

## 3. Run the live end-to-end test

The runner uses throwaway accounts (owner, holder, stranger) kept in `.live/accounts.json`, which is git-ignored, and prints what each needs.

```bash
python scripts/live_e2e.py --address 0xYOUR_ADDRESS --check-only   # tooling and fixture check
python scripts/live_e2e.py --address 0xYOUR_ADDRESS                # full run, roughly 60 to 80 minutes
```

Useful flags: `--fixture-base-url` (where `fixtures/live/` is publicly served), `--accept-seconds` (default 1500), `--return-seconds` (default 600), `--wait-seconds`, `--rpc-url`, `--resume`. State is kept in `.live/`, so an interrupted run resumes with `--resume` while its deadlines are still open; after a long interruption start a fresh run (the funded accounts are reused). The defaults assume every Studionet transaction takes roughly 40 seconds to finalize; slow networks should raise `--accept-seconds`.

The owner needs about 0.0018 GEN plus a margin and the holder about 0.012 GEN plus a margin; the runner tries `sim_fundAccount` first and otherwise prints the addresses to fund by hand.

Fixtures must be byte-exact over public HTTPS; `.gitattributes` disables line-ending conversion for them.

## 4. Read the report

Reports are written to `reports/live-e2e-<timestamp>.md` and `.json`. Expected outcomes:

| Loan | Scenario | Expected result |
| --- | --- | --- |
| A | claim on I2, I3, I4, I5; holder accepts I5 | I2 ruled `damaged` (I3 already in the inventory, I4 ordinary wear); deducted items I2 and I5 |
| B | owner report tries to instruct the reviewer | ruling `owner_foul`; nothing deducted; stake returned |
| C | owner's hosted report differs from its commitment | ruling `owner_evidence_tampered` (`owner:hash_mismatch`); stake forfeited to the holder |
| D | nobody claims | deposit returned after the claim window |
| E | owner releases the deposit early | deposit returned at once |
| F | holder never responds; owner claims I3 | reviewed on the owner's evidence alone; I3 ruled `damaged` |

Real LLM output is probabilistic: a `FAIL` on a semantic check (A, B, F rulings) is a finding to inspect, not automatically a contract bug. The withdrawal section compares real wallet balances before and after `withdraw_credit`; that is the only evidence about native-transfer delivery on your network.

## Known unverified assumptions

- `sim_fundAccount` funding may not exist on your Studionet endpoint; fund the printed addresses manually if the runner says so.
- Receipt field names in `genlayer-py` are parsed defensively and may need adjustment.
- The contract assumes transaction time is available through `datetime.now()` in consensus.
- The offline replay of the live scenario (`tests/direct/test_live_script.py`) mocks the web and the model; it proves the script and contract agree, not how real validators behave.

## Deployment record

| Field | Value |
| --- | --- |
| Version | 0.1.0 |
| Address | [`0x241D5b8077498eA5572264Ea060643C5Ee377aE1`](https://explorer-studio.genlayer.com/address/0x241D5b8077498eA5572264Ea060643C5Ee377aE1) |
| Deploy transaction | Deployed via GenLayer Studio on Studionet (see explorer address history) |
| Source SHA-256 | `0b3f399d0c17f1fc33d5f985a97cb15a028718e394315b51b1def873cd1824c2` (printed by `scripts/preflight.py`) |
| Live report | [`reports/live-e2e-20261126T144417Z.md`](../reports/live-e2e-20261126T144417Z.md) ([JSON](../reports/live-e2e-20261126T144417Z.json)) — 127/127 checks passed |
