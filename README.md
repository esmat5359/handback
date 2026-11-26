# Handback

**Handback is a standalone GenLayer Intelligent Contract for itemized deposit escrow on borrowed or rented property.** An owner commits an inventory document by SHA-256 and attaches a fixed deduction cap to every numbered item. The holder accepts by locking a deposit that covers all caps. When the property comes back, the owner may claim that some items were damaged or missing, backed by a hash-committed report. The holder can accept any subset of the claimed items immediately; whatever is left is disputed, and GenLayer validators read the committed inventory and reports and classify **only the disputed items**. Deterministic code, never model prose, decides which items are deducted, and the payout is the fixed per-item amount that both sides agreed before the loan began.

It is a reusable primitive for camera kits, tools, bikes, short lets, borrowed instruments and the like. It is not a legal service, not an insurer, and not a claim that an AI can inspect the real object: validators judge what the committed documents establish, nothing more.

## Why GenLayer?

A deterministic contract can enforce deadlines and payouts, but it cannot read an inventory and two free-text condition reports and decide whether "a deep crescent scratch visible as flare" is damage beyond fair wear, or whether "a small paint chip" was already on the tripod at handover. A single off-chain judge could quietly side with whoever pays it. Handback asks several validators to fetch the same hash-committed bytes and independently produce a **bounded** per-item classification, and the Equivalence Principle only lets a result stand when validators reproduce the same decisive findings. Free-form rationale is stored but never compared and never authorizes anything. The model is never asked about money: amounts are fixed in advance and never appear in the prompt.

## Lifecycle

```text
owner deposits credit, then opens a loan: inventory URL + hash, 1..8 items, per-item caps,
  deposit, stake (locked from credit), acceptance deadline, due date, claim window, response window
holder deposits credit and accepts by quoting the inventory hash   [deposit locked; terms are immutable]
  -> the loan period
holder hands back, optionally committing its own condition report (URL + hash) at that moment
owner either releases the deposit early, files a claim, or stays silent
  claim = item mask + a hash-committed owner report, filed after the return or the due date
  -> response window (claimed_at .. respond_by)
holder accepts any subset of the claimed items (settled on the spot) and may attach a report
  if it has none yet; items nobody accepts stay disputed
  -> review window (response .. +3 days)
owner or holder requests validator review of the disputed items
  -> ruled | owner_foul | holder_foul | evidence fault | still open (inconclusive / retryable)
anyone settles; everyone withdraws their own credit with withdraw_credit()
```

An offer nobody accepts can be cancelled by its owner at any time and expires on its own (anyone can return the stake after `accept_by`). Once accepted, a loan ends only by release, settlement after the claim window, or claim settlement.

## Money flow

All settlement is internal bookkeeping on a pull-payment ledger. No settlement step emits a transfer, so there is no partial-failure state across recipients; every party later withdraws only their own credit in a single transfer.

| Event | Effect |
| --- | --- |
| Nothing claimed before the claim window ends, or owner releases early | Whole deposit credited to the holder; stake credited back to the owner |
| Holder accepts a claimed item | That item's cap is credited to the owner at once |
| Validators rule a disputed item `damaged` | That item's cap is credited to the owner |
| Validators rule an item `not_damaged`, owner withdraws the claim, or no decisive finding exists | Nothing is deducted for the item (the owner carries the burden of proof) |
| Owner's committed evidence cannot be reproduced (hash mismatch, empty, oversized, not UTF-8) | Disputed items void **and the owner's stake is forfeited to the holder** |
| Holder's committed report cannot be reproduced, or stays unreachable for four probes | Every disputed item is deducted |
| Owner's evidence stays unreachable for four probes | Disputed items void, stake returned |
| Instruction attempt in the owner's inventory or report | Disputed items void, stake returned |
| Instruction attempt in the holder's report | Every disputed item is deducted |

The deduction for an item is always exactly its cap; the holder is always credited `deposit - deductions`. The liability invariant is `total_credits + total_locked_deposits + total_locked_stakes = every wei ever deposited and not yet withdrawn`. `get_accounting()` exposes it together with the contract balance.

Money penalties come only from *deterministic* faults (committed bytes that cannot be reproduced). A model's opinion about steering can only change which disputed items are deducted, never move a stake.

## Review rules

- Documents are fetched over HTTPS with exact byte hashes, a 30,000 byte limit and strict UTF-8. The inventory must label every item at the start of a line as `I1` .. `In`; `scripts/handback_tools.py` has a checker.
- The model answers `damaged`, `not_damaged` or `unclear` for each disputed item, plus `owner_instruction_attempt` and `holder_instruction_attempt` (`yes`/`no`/`unclear`) and one overall confidence. `damaged` means damage beyond fair wear and tear, or an item that did not come back.
- Below confidence 80 nothing is decisive. A review is **ruled** only if no label is `unclear`, both flags are `no` and confidence is at least 80. An owner instruction attempt (confidence at least 80) voids the disputed items; a holder instruction attempt deducts them; if both are flagged the owner's counts, because the owner carries the burden.
- Validators agree on a ruling only if they report the same set of damaged items, or the same foul class. Prose and exact confidence are ignored.
- The first decisive consensus verdict for a claim is final. Re-rolling cannot flip it.
- Quoted data is fenced by delimiters that the contract neutralizes inside the data, and the prompt tells the model to treat everything inside as untrusted.
- **Whose document failed decides who is blamed.** The owner's documents (inventory, claim report) are fetched first, then the holder's report. A non-2xx status, redirect or network error is never final: it is a retryable failure counted against the side that was supposed to serve the page. Four such failures for the owner side void the claim; four for the holder side lose the disputed items. A holder who is worried about hosting can simply submit no report.
- Each claim has at most 8 review calls and 3 semantic assessments, with a cooldown that doubles from 60 seconds to one hour. Only the owner and the holder can request a review, so a stranger cannot burn the budget.
- A claim with no decisive finding when its budget is exhausted or its review window (3 days after the response) ends settles with no deduction for the disputed items.

## Security boundaries and known limits

- SHA-256 proves byte identity, not truth or authorship. URL admission cannot pin DNS or prohibit every redirect. Use immutable hosting (a commit-pinned raw file, an IPFS gateway); a host that edits bytes after commitment costs its owner a stake.
- Reports are text. Validators cannot see photographs. Both parties are expected to describe condition per item and to refer to items as I1..In.
- The holder's report is committed at hand-back, before any claim exists. A report attached later with the response is allowed only when none was committed before and can answer the owner's report; the owner cannot reply to it.
- A claim can be filed for an item that was never returned; the owner's report must say so, and `damaged` covers "did not come back". The contract has no way to confirm physical handover.
- An owner can file a claim to delay the holder's deposit, but only for the response window (at most 7 days) plus the review window (3 days), and the holder can end the wait at once by responding and requesting review.
- Equivalence consensus is probabilistic. Models can disagree with a fixture; a retry is allowed and a decisive verdict is then final.
- Withdrawal uses `emit_transfer`. The contract cannot observe whether a downstream credit reaches a recipient, so a recipient that is a contract rejecting plain transfers could not be re-credited. The live test therefore compares real wallet balances before and after withdrawal and records the result in its report; do not treat delivery as proven until that report says so for your network.

## Build and verification

```bash
python -m pip install -r requirements.txt
python scripts/preflight.py --write-schema    # source rules, genvm-lint, ABI, and every Direct Mode test
python scripts/preflight.py                   # same gate without rewriting the ABI file
```

The gate pins `GENVM_VERSION=v0.2.16`, requires the source header to match `VERSION`, requires zero skipped tests, and prints the contract hash. The suite covers input validation (every malformed offer), exact deadline boundaries, role checks, review outcomes, fault attribution, the validator equivalence rules, adversarial text and double-spend attempts, an exhaustive payout table for every claim/acceptance combination, a seeded random walk that checks conservation and state-machine invariants after every few steps, and an offline replay of the live scenario against the contract.

At the time of writing the gate reports 273 passing Direct Mode tests, zero skipped, `genvm-lint` clean, contract source SHA-256 `0b3f399d0c17f1fc33d5f985a97cb15a028718e394315b51b1def873cd1824c2`. A 35-mutant mutation check of the contract against the suite left no survivor.

`scripts/handback_tools.py` has the client helpers (`sha256_hex`, `caps_text`, `mask_of`, `items_of`, `check_inventory`).

## Live end-to-end test on Studionet

```bash
python scripts/live_e2e.py --address 0xYOUR_DEPLOYED_ADDRESS --check-only
python scripts/live_e2e.py --address 0xYOUR_DEPLOYED_ADDRESS
```

It runs six loans between one owner and one holder with real validators and a real LLM (mixed claim, steering attempt, tampered report, unclaimed loan, early release, silent holder), withdraws all credit as native GEN, and writes a Markdown and JSON report under `reports/`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the Codespaces steps and [docs/DESIGN.md](docs/DESIGN.md) for the reasoning behind each rule.

## Deployment status

Deployed on GenLayer **Studionet** at [`0x241D5b8077498eA5572264Ea060643C5Ee377aE1`](https://explorer-studio.genlayer.com/address/0x241D5b8077498eA5572264Ea060643C5Ee377aE1). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and the full live verification report ([`reports/live-e2e-20261126T144417Z.md`](reports/live-e2e-20261126T144417Z.md), 127/127 checks passed).
