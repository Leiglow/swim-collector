# swim-collector

Reads every public bathing-water feed for Britain and Ireland every half hour, works out
whether it looks safe to swim at each of 941 designated bathing waters, and publishes one
small JSON snapshot.

It runs here rather than on the website's own hosting for a dull reason: a free Cloudflare
Worker gets 10ms of CPU per request, and parsing 18,600 storm-overflow records is far past
that. GitHub Actions has no such limit.

## What it reads

| Source | What it gives | Refresh |
| --- | --- | --- |
| Environment Agency (England) | daily pollution risk forecast, open pollution incidents, active suspensions, site register | daily / as they happen |
| Natural Resources Wales | the same, for 114 Welsh bathing waters | daily |
| SEPA (Scotland) | daily water quality prediction — for 30 of Scotland's 90 beaches | daily |
| DAERA (Northern Ireland) | latest bacterial sample result per site | weekly in season |
| EPA / beaches.ie (Republic of Ireland) | statutory bathing prohibitions and advisory notices | as issued |
| 9 English water companies, Dŵr Cymru, Scottish Water | live storm overflow status for 18,600 monitored outfalls | 5–60 min |
| Open-Meteo | rainfall over the last 24 and 48 hours | hourly model |

All of it is published openly. Nothing here needs an API key.

## How it decides

An official warning is the answer on its own — a legal prohibition or a "do not bathe" notice
is never averaged against anything else. Failing that, a monitored storm overflow discharging
within 2km counts against a beach, one that discharged in the last 48 hours counts for less,
and heavy rain counts at beaches the regulator has flagged as rain-affected.

Four rules the code is built around, each of which it broke at least once during development:

1. **Never show green because something failed.** A dead monitor, a feed that is down, or a
   season that has ended all produce "can't say", never "no warnings". Every beach carries a
   list of what was actually checked, and the page shows it.
2. **An official warning outranks anything calculated here.**
3. **Last season's classification is not evidence about today's water.** It can add a warning;
   it can never justify a green tick.
4. **Say when, and say whether a number was measured or modelled.**

## What it cannot tell you

- **Ireland has no storm overflow monitoring at all**, north or south. There is no programme
  to read. An empty spill list for an Irish beach means nobody is measuring.
- **Scotland forecasts 30 of its 90 bathing waters.** The rest have last year's rating only.
- **Only monitored overflows appear.** Unmonitored pipes, farm run-off and misconnected drains
  do not.
- **Distance is not a model.** Tides and currents decide what actually reaches a beach. Where
  Southern Water publishes its own tidal assessment, that is used instead.

## Files

    swim_sources.py           every upstream URL, with its traps documented next to it
    collect_swim.py           the half-hourly job: read feeds, decide, publish
    build_swim_register.py    the monthly job: rebuild the beach register and work out
                              which outfalls sit near which beach
    swim_irishgrid.py         Irish Grid to WGS84, because beaches.ie gives a latitude for
                              only 153 of its 240 sites
    check_irish_grid.py       proves that conversion against the 153 that publish both
                              (median error 0m, 152 of 153 within 100m)

The register (`sites.json`, `nearby.json`, `outfalls.json`) is fetched at run time from wherever
`SWIM_DATA_BASE` points, so there is one copy of it rather than two drifting apart. Build it
yourself with `build_swim_register.py`.

## Running it

    python3 collect_swim.py              # writes a snapshot locally
    python3 collect_swim.py --publish    # and posts it to the site

Publishing needs `SWIM_INGEST_URL` and a bearer token in `SWIM_INGEST_TOKEN`; reading the
register needs `SWIM_DATA_BASE`. Standard library only — no dependencies to install.

In CI there is no stored password: the workflow asks GitHub for a short-lived signed token
proving which repository it is, and the receiving endpoint verifies that signature against
GitHub's public keys. Nothing secret exists on either side.

## Licence and attribution

The code is MIT. The data is not mine: it is published by the Environment Agency, Natural
Resources Wales, SEPA, DAERA, the EPA in Ireland, and the water companies, and it carries their
licences — most of it the Open Government Licence v3.0, the water company feeds CC BY 4.0.

Nothing here is an official all-clear. If a sign at the beach says otherwise, believe the sign.
