"""The offline client helpers must agree with the contract's conventions."""

import pytest

from scripts.handback_tools import (
    caps_text, check_inventory, deduction, items_of, loan_key, mask_of, sha256_hex,
)

GOOD = "# Inventory\nI1: Body - fine.\n- I2: Lens - clean.\n* I3: Bag - zipped.\n"


def test_mask_and_item_numbers_round_trip():
    assert mask_of([1, 3]) == 0b101 and items_of(0b101) == [1, 3]
    assert mask_of([]) == 0 and items_of(0) == []
    assert items_of(mask_of([8, 1, 4])) == [1, 4, 8]
    for bad in (0, 9, -1):
        with pytest.raises(ValueError):
            mask_of([bad])


def test_caps_text_and_deduction():
    assert caps_text([5, 7]) == "5,7"
    with pytest.raises(ValueError):
        caps_text([])
    with pytest.raises(ValueError):
        caps_text([1] * 9)
    with pytest.raises(ValueError):
        caps_text([0, 1])
    assert deduction([10, 20, 30], 0b101) == 40


def test_hash_and_key_formats():
    assert sha256_hex(b"") == "0x" + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert loan_key("0xABC", "L1") == "0xabc:L1"


def test_inventory_checker():
    assert check_inventory(GOOD, 3) == []
    assert "I4" in " ".join(check_inventory(GOOD, 4))
    assert check_inventory(GOOD, 2) and "outside" in " ".join(check_inventory(GOOD, 2))
    assert "2 lines" in " ".join(check_inventory(GOOD + "I1: duplicate\n", 3))
    assert check_inventory("I1 sits mid-line: no\n  see I2 here\n", 2)
