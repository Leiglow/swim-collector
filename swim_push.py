"""Tell people when a beach they follow turns bad.

THE RULE THIS WHOLE THING HANGS ON. Somebody who has switched notifications on
reads silence as "nothing is wrong". So a sender that quietly stops working does
not fail to deliver a message — it delivers a false all clear, to a pocket, on a
site about whether it is safe to swim. Every decision below follows from that:

  * only WARNINGS are ever sent. Never "your beach is fine again". An all clear
    delivered to a pocket is the reassurance this site refuses to give, and it
    would teach people that silence means good news, which is precisely what
    makes a broken sender dangerous rather than merely useless.
  * every run reports back even when it sent nothing, so the site can tell
    "we checked and there was nothing to say" from "the checking has stopped".
    Silence then becomes visible instead of reassuring.
  * only TRANSITIONS are sent — a beach that was not warned and now is. A beach
    that is still warned tomorrow is not news, and a beach that flaps is capped
    at one message a day.
  * the crypto is pywebpush's, not mine. Web Push signing and payload encryption
    have to be exactly right, and wrong crypto here fails silently, which is the
    failure that reads as an all clear.

Sending is skipped entirely unless VAPID_PRIVATE is set, so a runner without the
secret does everything else as normal.
"""

import json
import os
import ssl
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

WARNED = ("avoid", "advised")
WORD = {"avoid": "Do not swim today", "advised": "Advised against today"}

CTX = ssl.create_default_context()
TIMEOUT = 30


def _token(audience):
    """A short-lived GitHub token proving which repository this is.

    Same mechanism the collector already uses to publish: no shared secret to
    store or leak, and a token minted for one endpoint is no use at another.
    """
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not tok:
        return None
    req = urllib.request.Request(
        url + "&audience=" + audience,
        headers={"Authorization": "bearer " + tok, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        return json.load(r).get("value")


def _api(base, token, method="GET", body=None, query=""):
    url = base.rstrip("/") + "/push" + query
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "swim-collector",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def newly_warned(previous, current):
    """Beaches that were not warned and now are.

    A beach missing from the previous snapshot counts as NOT a transition. The
    first run after a gap would otherwise announce every warned beach in the
    country at once, which is both useless and the fastest way to be muted — and
    being muted is how a real warning goes unread later.
    """
    if not previous:
        return {}
    out = {}
    for sid, row in (current or {}).items():
        now = (row or {}).get("v")
        if now not in WARNED:
            continue
        was = (previous.get(sid) or {}).get("v")
        if was is None or was in WARNED:
            continue
        out[sid] = now
    return out


def _send(sub, payload, private_key, contact):
    from pywebpush import webpush, WebPushException     # noqa: PLC0415
    webpush(
        subscription_info={
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        },
        data=json.dumps(payload),
        vapid_private_key=private_key,
        vapid_claims={"sub": contact},
        ttl=3600,          # a warning nobody's phone collected within the hour
                           # is stale; the site is the place to look by then
    )


def run(previous_sites, current_sites, places, base_url=None, dry_run=False,
        test=False):
    """Send what needs sending. Returns a short summary for the run log.

    places: {site id: {"name": ..., "slug": ...}} — only for the wording and the
    link. Nothing about a person is passed in or kept.
    """
    base = base_url or os.environ.get("SWIM_DATA_BASE") or ""
    private_key = os.environ.get("VAPID_PRIVATE")
    contact = os.environ.get("VAPID_CONTACT", "mailto:hello@caniswim.co.uk")
    if not base:
        return "push: no SWIM_DATA_BASE — skipped"
    if not private_key:
        return "push: VAPID_PRIVATE not set — nothing sent, which is correct " \
               "until the site has been switched on"

    # A way to prove delivery without waiting for a beach to actually turn bad.
    # Worth having permanently, not just on the day this was built: "did the
    # notifications stop working" is otherwise unanswerable except by waiting for
    # a warning and seeing whether one arrives, which is the wrong way round on a
    # safety site. Says plainly that it is a test, so nobody reads it as real.
    changed = {} if test else newly_warned(previous_sites, current_sites)

    try:
        token = _token("swim-push")
        if not token:
            return "push: no OIDC token — skipped"
        listing = _api(base, token, query="?list=1")
    except Exception as e:                                # noqa: BLE001
        # Never fail the collector over this. The readings matter more than the
        # notifications, and the heartbeat missing is itself the signal.
        return "push: could not read subscriptions (%s)" % str(e)[:120]

    subs = listing.get("subscriptions") or []
    told = listing.get("told") or {}
    today = date.today().isoformat()

    # One message a day per beach, however much it flaps. A test bypasses that,
    # since the whole point of it is to arrive.
    fresh = changed if test else {s: v for s, v in changed.items()
                                  if told.get(s) != today}

    sent = gone = failed = 0
    dead = []
    if (fresh or test) and not dry_run:
        for sub in subs:
            mine = [s for s in (sub.get("sites") or []) if s in fresh]
            if test:
                payload = {"title": "Test: notifications are working",
                           "body": "This is a test, not a warning. Nothing is "
                                   "wrong with any of your beaches.",
                           "url": "/", "tag": "swim-test"}
                try:
                    _send(sub, payload, private_key, contact)
                    sent += 1
                except Exception as e:                # noqa: BLE001
                    msg = str(e)
                    if "404" in msg or "410" in msg:
                        dead.append(sub.get("endpoint")); gone += 1
                    else:
                        failed += 1
                        print("      test send failed: %s" % msg[:200])
                continue
            if not mine:
                continue
            first = mine[0]
            place = places.get(first) or {}
            title = ((place.get("name") or "A beach you follow")
                     + ": " + WORD.get(fresh[first], "warning"))
            if len(mine) > 1:
                body = ("and %d other%s you follow. Tap to see why, and when it "
                        "was checked." % (len(mine) - 1, "" if len(mine) == 2 else "s"))
            else:
                body = "Tap to see why, and when it was last checked."
            slug = place.get("slug")
            payload = {"title": title, "body": body,
                       "url": ("/beach/%s/" % slug) if slug else "/",
                       "tag": "swim-" + first}
            try:
                _send(sub, payload, private_key, contact)
                sent += 1
            except Exception as e:                        # noqa: BLE001
                msg = str(e)
                # 404 and 410 mean the browser threw the subscription away.
                if "404" in msg or "410" in msg:
                    dead.append(sub.get("endpoint"))
                    gone += 1
                else:
                    failed += 1

    for s in fresh:
        told[s] = today

    try:
        _api(base, token, method="POST",
             body={"told": told, "sent": sent, "gone": gone,
                   "dead": [d for d in dead if d]})
    except Exception as e:                                # noqa: BLE001
        return ("push: sent %d but could not report back (%s)"
                % (sent, str(e)[:100]))

    if test:
        return ("push TEST: %d sent, %d dead, %d failed, %d subscriptions"
                % (sent, gone, failed, len(subs)))
    return ("push: %d newly warned, %d after the daily cap, %d sent, %d dead, "
            "%d failed, %d subscriptions"
            % (len(changed), len(fresh), sent, gone, failed, len(subs)))
