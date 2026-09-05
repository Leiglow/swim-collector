"""Guards on the rules that decide what a beach is told.

Run before every collection. These are not tests of the feeds — they are tests
of the small number of places where a mistake would put a green tick on a page
that has not earned one, which is the one failure this site exists to avoid.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect_swim as m                                  # noqa: E402


class Feed(object):
    """Just the one attribute the code under test reads."""

    def __init__(self, ok):
        self.ok = ok


def test_a_stale_feed_is_not_an_all_clear():
    """A feed nothing has been restamped in comes in as blind, not as clean.

    stale_check marks the feed unhealthy, but its rows still arrive saying
    "Status: 0". Folded in unchanged they count under "monitored storm
    overflows within 2km" and that alone earns a green verdict — resting on
    data the same run has already judged dead.
    """
    rows = {"TW:1": m.state(False, None, None)}
    out = {}
    m.merge_spills(out, rows, Feed(False))
    assert out["TW:1"]["offline"] is True

    out = {}
    m.merge_spills(out, rows, Feed(True))
    assert out["TW:1"]["offline"] is False


def test_a_discharge_survives_its_feed_going_quiet():
    """Refusing the green tick must not also lose the warning.

    A publisher can go quiet mid-discharge. Dropping the feed entirely would be
    the simpler fix and the wrong one: losing a reported discharge is a worse
    failure than declining to call a beach clear.
    """
    rows = {"TW:2": m.state(True, None, None)}
    out = {}
    m.merge_spills(out, rows, Feed(False))
    assert out["TW:2"]["now"] is True
    assert out["TW:2"]["offline"] is True


def test_merging_does_not_edit_the_feed_it_read():
    """The rows belong to the feed object, which prints its own counts later."""
    rows = {"TW:1": m.state(False, None, None)}
    m.merge_spills({}, rows, Feed(False))
    assert rows["TW:1"]["offline"] is False


def test_every_reason_type_the_collector_emits_has_a_rank():
    """The alerts feed sorts reasons by type, and an absent name is silent.

    It ranked "spill-now", which nothing emits, and had no entry for "spill" —
    a discharge happening now, and usually the reason the verdict is Do not
    swim. It fell to the default and sorted below rain, so the feed published
    "Do not swim" with a rainfall figure under it while the beach page led with
    the sewage. Nothing failed; a name was simply missing.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "collect_swim.py"), encoding="utf-8").read()
    block = src.split("WHY_ORDER = {", 1)[1].split("}", 1)[0]
    ranked = set(re.findall(r'"([a-z-]+)"', block))
    missing = m.KNOWN_WHY_TYPES - ranked
    assert not missing, "no rank for %s" % sorted(missing)
    invented = ranked - m.KNOWN_WHY_TYPES
    assert not invented, "ranks a type nothing emits: %s" % sorted(invented)


def test_a_discharge_now_outranks_rain():
    """The specific pairing that was wrong, in the order the feed applies."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "collect_swim.py"), encoding="utf-8").read()
    block = src.split("WHY_ORDER = {", 1)[1].split("}", 1)[0]
    order = dict((k, int(v)) for k, v in re.findall(r'"([a-z-]+)":\s*(\d+)', block))
    assert order["spill"] < order["rain"], order
    assert order["warning"] < order["spill"], order


def test_a_dead_monitor_is_never_counted_as_not_discharging():
    """state()'s own promise, in one line."""
    off = m.state(False, None, None, offline=True)
    assert off["offline"] is True and off["now"] is False


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  ok    %s" % name)
        # Every exception, not only AssertionError. A test that raises a
        # KeyError because the thing it looks for has been renamed is a failing
        # test, and it used to kill the whole run instead of being reported —
        # which on a run that gates a deploy is the wrong way round.
        except Exception as e:                            # noqa: BLE001
            fails += 1
            print("  FAIL  %s  %s" % (name, e))
    print("%d checked, %d failed" % (
        len([k for k in globals() if k.startswith("test_")]), fails))
    sys.exit(1 if fails else 0)
