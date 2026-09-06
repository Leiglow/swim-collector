"""Guards on who gets told, and who is still owed a warning.

A notification that does not arrive is the quietest failure this project has:
nobody is looking at a screen, so there is nothing to notice. These pin the two
rules that decide whether it is retried.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swim_push as P                                     # noqa: E402


class Calls(object):
    """Stands in for the site's push API. Records what was posted back."""

    def __init__(self, listing):
        self.listing = listing
        self.posted = None

    def __call__(self, base, token, method="GET", body=None, query=""):
        if method == "POST":
            self.posted = body
            return {"ok": True}
        return self.listing


def run(monkey, sends, listing, prev, now):
    """Drive send_warnings with a stubbed transport, and return what it posted."""
    calls = Calls(listing)
    monkey["_api"] = calls
    P._api = calls
    P._token = lambda a: "tok"
    P._key_fault = lambda k: None

    def send(sub, payload, key, contact):
        outcome = sends.pop(0) if sends else "ok"
        if outcome != "ok":
            raise RuntimeError(outcome)

    P._send = send
    os.environ["VAPID_PRIVATE"] = "x" * 43
    P.run(prev, now, {"E:1": {"name": "Beach One", "slug": "beach-one"}},
                    base_url="https://example.invalid")
    return calls.posted


SUB = {"endpoint": "https://push.example/1", "sites": ["E:1"],
       "keys": {"p256dh": "k", "auth": "a"}}
PREV = {"E:1": {"v": "ok"}}
NOW = {"E:1": {"v": "avoid"}}


def test_a_failed_send_is_not_recorded_as_told():
    """The bug: any error that is not 404/410 still marked the beach told."""
    posted = run({}, ["boom 500"], {"subscriptions": [SUB], "told": {}, "owed": {}},
                 PREV, NOW)
    assert "E:1" not in (posted.get("told") or {}), posted


def test_a_failed_send_is_carried_forward():
    """And it has to be written down, or nothing will ever notice again."""
    posted = run({}, ["boom 500"], {"subscriptions": [SUB], "told": {}, "owed": {}},
                 PREV, NOW)
    assert "E:1" in (posted.get("owed") or {}), posted
    assert posted["owed"]["E:1"]["v"] == "avoid", posted


def test_a_delivered_send_is_told_and_owes_nothing():
    posted = run({}, ["ok"], {"subscriptions": [SUB], "told": {}, "owed": {}},
                 PREV, NOW)
    assert "E:1" in (posted.get("told") or {}), posted
    assert "E:1" not in (posted.get("owed") or {}), posted


def test_one_subscriber_success_does_not_cancel_another_subscriber_warning():
    """The bug: `delivered` was a set of BEACH ids, added to inside the
    per-subscription loop, so the first phone that accepted a warning marked
    that beach told for everybody — and the person whose send failed never got
    it, on this run or any later one."""
    two = [SUB, {"endpoint": "https://push.example/2", "sites": ["E:1"],
                 "keys": {"p256dh": "k", "auth": "a"}}]
    posted = run({}, ["ok", "boom 500"],
                 {"subscriptions": two, "told": {}, "owed": {}}, PREV, NOW)
    assert "E:1" not in (posted.get("told") or {}), posted
    assert "E:1" in (posted.get("owed") or {}), posted


def test_a_dead_subscription_does_not_hold_a_warning_open():
    """404/410 means the browser threw the subscription away, so there is no
    phone left to owe. One live success plus one dead endpoint is delivered."""
    two = [SUB, {"endpoint": "https://push.example/2", "sites": ["E:1"],
                 "keys": {"p256dh": "k", "auth": "a"}}]
    posted = run({}, ["ok", "410 gone"],
                 {"subscriptions": two, "told": {}, "owed": {}}, PREV, NOW)
    assert "E:1" in (posted.get("told") or {}), posted
    assert "E:1" not in (posted.get("owed") or {}), posted


def test_an_owed_warning_is_retried_while_the_beach_is_still_warned():
    """No transition to find on the next run — that is the whole point of owed."""
    owed = {"E:1": {"v": "avoid", "at": "2026-09-05T09:00:00Z"}}
    posted = run({}, ["ok"],
                 {"subscriptions": [SUB], "told": {}, "owed": owed},
                 NOW, NOW)                      # warned in BOTH snapshots
    assert "E:1" in (posted.get("told") or {}), posted
    assert "E:1" not in (posted.get("owed") or {}), posted


def test_an_owed_warning_is_dropped_once_the_beach_is_clear():
    """Nobody wants a notification about a warning that is over."""
    owed = {"E:1": {"v": "avoid", "at": "2026-09-05T09:00:00Z"}}
    posted = run({}, ["ok"],
                 {"subscriptions": [SUB], "told": {}, "owed": owed},
                 NOW, {"E:1": {"v": "ok"}})
    assert "E:1" not in (posted.get("owed") or {}), posted
    assert "E:1" not in (posted.get("told") or {}), posted


def test_a_dead_subscription_owes_nothing():
    """404/410 means the phone is gone. There is nobody left to tell."""
    posted = run({}, ["410 Gone"], {"subscriptions": [SUB], "told": {}, "owed": {}},
                 PREV, NOW)
    assert "E:1" not in (posted.get("owed") or {}), posted


if __name__ == "__main__":
    fails = 0
    names = [k for k in sorted(globals()) if k.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print("  ok    %s" % name)
        # Every exception, not only AssertionError. A test that raises a
        # KeyError because the thing it looks for has been renamed is a failing
        # test, and it used to kill the whole run instead of being reported —
        # which on a run that gates a deploy is the wrong way round.
        except Exception as e:                            # noqa: BLE001
            fails += 1
            print("  FAIL  %s  %s" % (name, str(e)[:200]))
    print("%d checked, %d failed" % (len(names), fails))
    sys.exit(1 if fails else 0)
