# scanner-ingestion-easybet

Easybet (easybet.co.za) odds scraper for the MadunaCapital arbitrage scanner. One of several independent per-bookmaker repos, kept maximally decoupled from the others: a bug, dependency bump, or bad release here can't touch any other bookmaker's scraper.

## How it works

Easybet's own site (`easybet.co.za`) is a thin Vue SPA served via CloudFront/nginx (not Cloudflare) with no odds data of its own -- its `/sports` page embeds an iframe pointing at a third-party sportsbook platform ("AdvBet" / `adv.bet` / `advbet.com`). That platform exposes a plain, unauthenticated REST endpoint the embedded sportsbook app itself calls:

```
GET https://sportsbook-sa01-backend.advbet.com/distributor/api/organizations/<org-uuid>/events
    ?view=full&filter.types=match&filter.sports=football&cursor.limit=100
```

Plain `httpx` GET, no TLS impersonation, no stealth browser, no Cloudflare bypass, no auth token, no cookies: none of that is needed. `<org-uuid>` is Easybet's static organization identifier on the AdvBet platform, not a session token — see `scraper.py`'s module docstring for the full (one-time, by-hand) discovery chain that found it, starting from `easybet.co.za/config.json`. Polls on a plain, fixed 45-second interval by default — no jitter, no randomization to look human.

Covers both soccer (`filter.sports=football`) and rugby (`filter.sports=rugby`) — South Africa's #1 and #2 sports by popularity — fetched as two separate plain GETs to the same endpoint each poll cycle (see `scraper.py`'s module docstring for why they're kept as separate requests rather than one combined query). Maps the `winner3` (three-way 1X2) market onto the universal `moneyline` market key for both sports, same scope as Betway ZA and WSB (each currently handles one core market only). AdvBet exposes several hundred other market types (totals, handicaps, BTTS, etc.) per event, out of scope for now.

Depends on:
- [scanner-ingestion](https://github.com/MadunaCapital/scanner-ingestion) (base install only, no `stealth` extra — this adapter doesn't need it) for `BaseScraper`
- [scanner-schemas](https://github.com/MadunaCapital/scanner-schemas) for `OddsEvent`/`MarketOdds`

Both pulled in as git dependencies in `requirements.txt`, same pattern as every other repo in this project.

## Status

Working and verified live for both sports: `EasybetScraper().fetch_raw_odds(sport)` + `.to_odds_events()` returns real current matches and sane decimal odds (confirmed via plain `curl`, e.g. home/draw/away 1.85/2.9/3.4 on a real Liga 2 soccer fixture, and 1.32/22/3.45 on a real Top 14 rugby fixture). Passing tests, including the fixture/tournament join, market-state filtering (hidden/suspended/closed), incomplete-price handling, the rugby market mapping, and the polling loop (fixed interval, per-sport fetch failures don't withhold the other sport's odds, and a fully failed cycle stays silent and recoverable).

## Local dev

```
pip install -r requirements.txt
pytest
```

## Scope note

This adapter intentionally only reads what Easybet's embedded sportsbook app already fetches publicly, at a reasonable polling interval — no authentication bypass, no anti-bot evasion. Terms of Service exposure for scraping public data is a real but different (lower-severity, contractual rather than computer-misuse) question than the Cybercrimes Act question that applies to defeating security measures.
