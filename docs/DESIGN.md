# Handback design

## State machine

```text
            open_loan                accept_loan               file_claim
 (none) ---------------> offered ---------------> active ---------------> claimed
                          |  |                      |  |                      |
   cancel_offer / expire  |  |                      |  | settle_loan          | settle_loan / respond (all accepted)
                          v  |                      |  | release_loan         | withdraw_claim
                     cancelled                      |  v                      v
                                                    +---------------------> settled
```

`hand_back` does not change the state; it records `returned_at` and an optional holder report. Terminal states are `settled` and `cancelled`.

## Time windows

| Window | Opens | Closes |
| --- | --- | --- |
| Acceptance | `open_loan` | `accept_by` |
| Loan period | acceptance | `return_by` |
| Hand back | acceptance | `return_by + claim_window` (exclusive) |
| Claim | the return, or `return_by` if there was none | `max(return_by, returned_at) + claim_window` (exclusive) |
| Response | `claimed_at` | `claimed_at + respond_window` (exclusive) |
| Review | the response, or the end of the response window | 3 days later (exclusive) |

Every window has a 120 second minimum so tests and live runs stay practical; maxima are 30 days to accept, 90 days of loan term, 14 days of claim window and 7 days of response window. A late hand-back moves the claim deadline so the owner always gets a full window after the property actually returns; a holder who never hands back cannot stretch the deadline.

## Why per-item caps

Letting a model price damage would make money depend on prose. Fixing one cap per item before the loan means validators answer a small factual question per item, the payout is a sum of constants, and a holder can compute the worst case before accepting. Caps are validated at offer time: between 1 and 8 items, each at least 10^13 wei, summing to at most the deposit.

## Evidence ordering

The owner's inventory is committed at offer time and quoted back by the holder when accepting. The holder's report is committed at hand-back, before any claim exists, so it cannot be tailored to a claim. The owner's report is committed in the claim itself. A holder with no report may attach one with its response, once.

## Burden of proof and defaults

Whenever the process ends without a decisive finding (budget exhausted, review window over, owner evidence unreachable), the disputed items are not deducted. Only the holder's own actions can move the default the other way: a tampered or persistently unreachable holder report, or an instruction attempt in it. Accepted items are never affected by any of this.

## Fault attribution

| Observation | Treated as |
| --- | --- |
| Non-2xx, redirect, network error on an owner document | Retryable; the fourth strike voids the disputed items |
| Same on the holder report | Retryable; the fourth strike deducts the disputed items |
| Hash mismatch, empty, oversized or non-UTF-8 owner bytes | Final; disputed items void, stake forfeited to the holder |
| Same on the holder report | Final; disputed items deducted |
| Model output that fails validation, model outage | Retryable, charged to the budget, blames nobody |

Validators must blame the same side with the same reason for a failure or an evidence fault to stand, because the attribution feeds the strike counters and the stake.

## Equivalence

A result is `analysis`, `failure` or `evidence_issue`. Validators re-run the whole observation and agree only on:

- `analysis` with the same outcome class; for `ruled`, the same set of damaged disputed items; for a foul class, nothing more (the flags define it); for `inconclusive`, nothing more.
- `failure` with the same reason (owner side, holder side, or generic).
- `evidence_issue` with the same party and reason.

A leader result that is not structurally valid is rejected before comparison.

## Review bounds

At most 8 calls and 3 semantic assessments per claim. The cooldown after call `n` is `min(60 * 2^(n-1), 3600)` seconds. Strike counters are independent of the semantic budget: a transport failure consumes a call but not an assessment. Only the two parties can request a review.

## Pull-payment ledger

`deposit_credit` credits the sender; stakes and deposits are spent from credit and locked; every outcome credits the ledger; `withdraw_credit` zeroes the caller's credit before emitting one transfer. Offers lock only the stake. A deposit is locked from acceptance until settlement.
