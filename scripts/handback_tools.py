"""Client-side helpers for Handback owners, holders and operators.

Everything here is pure and offline. The contract identifies items by bit
position in a mask (bit 0 is item I1) and wants deduction caps as a comma
separated list of wei amounts, so these helpers keep that bookkeeping out of
hand-written scripts.
"""

from __future__ import annotations

import hashlib

MAX_ITEMS = 8


def sha256_hex(raw: bytes) -> str:
    """0x-prefixed lowercase SHA-256, the format the contract expects."""
    return "0x" + hashlib.sha256(raw).hexdigest()


def loan_key(owner_address: str, loan_id: str) -> str:
    return owner_address.lower() + ":" + loan_id


def caps_text(caps: list[int]) -> str:
    """Comma separated per-item caps in wei, exactly as open_loan expects."""
    if not 1 <= len(caps) <= MAX_ITEMS:
        raise ValueError(f"between 1 and {MAX_ITEMS} items are supported")
    if any(int(cap) < 1 for cap in caps):
        raise ValueError("every cap must be positive")
    return ",".join(str(int(cap)) for cap in caps)


def mask_of(items: list[int]) -> int:
    """Mask for 1-based item numbers: [1, 3] -> 0b101."""
    mask = 0
    for item in items:
        if not 1 <= item <= MAX_ITEMS:
            raise ValueError(f"item numbers run from 1 to {MAX_ITEMS}")
        mask |= 1 << (item - 1)
    return mask


def items_of(mask: int) -> list[int]:
    """1-based item numbers set in a mask: 0b101 -> [1, 3]."""
    return [position + 1 for position in range(MAX_ITEMS) if (int(mask) >> position) & 1]


def deduction(caps: list[int], mask: int) -> int:
    """Wei the owner receives if exactly the items in `mask` are deducted."""
    return sum(caps[item - 1] for item in items_of(mask))


def check_inventory(text: str, item_count: int) -> list[str]:
    """Problems that would make an inventory ambiguous for the validators.

    The review prompt refers to items as I1..In, so the inventory must label each
    item with exactly that token at the start of a line. Returns a list of problems;
    an empty list means the layout is usable.
    """
    import re

    problems = []
    for number in range(1, item_count + 1):
        pattern = rf"(?m)^\s*(?:[-*]\s*)?I{number}\b"
        hits = len(re.findall(pattern, text))
        if hits == 0:
            problems.append(f"item I{number} is not listed at the start of any line")
        elif hits > 1:
            problems.append(f"item I{number} appears at the start of {hits} lines")
    extra = sorted({int(n) for n in re.findall(r"(?m)^\s*(?:[-*]\s*)?I(\d+)\b", text)
                    if int(n) > item_count or int(n) < 1})
    for number in extra:
        problems.append(f"item I{number} is outside the declared range I1..I{item_count}")
    return problems
