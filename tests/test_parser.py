"""Position-reply parser tests — pure regex, no Qt needed."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from valve_controller import _POS_RE


def assert_match(text, expected):
    m = _POS_RE.search(text)
    assert m is not None, f"no match in {text!r}"
    assert int(m.group(1)) == expected, f"expected {expected}, got {m.group(1)} in {text!r}"


def assert_no_match(text):
    assert _POS_RE.search(text) is None, f"should not match {text!r}"


def main():
    # canonical
    assert_match("Position is = 3", 3)
    assert_match("Position is = 12", 12)
    assert_match("Position is = 0", 0)

    # case / spacing variations
    assert_match("position is = 5", 5)
    assert_match("POSITION IS = 6", 6)
    assert_match("  Position is=4  ", 4)
    assert_match("Position is  =  7", 7)
    assert_match("Position\tis\t=\t8", 8)

    # embedded in line endings
    assert_match("Position is = 2\r\n", 2)
    assert_match("\r\nPosition is = 9\r\n", 9)

    # noise around the payload
    assert_match("ack;Position is = 1;ok", 1)

    # negatives
    assert_no_match("garbage")
    assert_no_match("Pos = 3")
    assert_no_match("Position is 3")          # missing '='
    assert_no_match("")
    assert_no_match("OK")

    print("test_parser: 14 assertions PASSED")


if __name__ == "__main__":
    main()
