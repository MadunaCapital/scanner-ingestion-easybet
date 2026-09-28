import asyncio

import pytest

from easybet import EasybetScraper

# Shape captured from a real, plain GET to AdvBet's public sportsbook
# events endpoint (see scraper.py's docstring). Trimmed to one match's
# worth of records (dropped the huge per-event player/competitor
# translation lists and most of the ~380 other market types), field names
# and values otherwise unchanged from the real response.
SAMPLE_RAW_PAYLOAD = {
    "result": [
        {
            "fixture": {
                "uuid": "012bb9a9-56c3-5060-8116-e8489808eeb7",
                "type": "match",
                "categoryUuid": "7b22f80b-2921-5af8-9b6b-195f7d975752",
                "tournamentUuid": "8debb4d7-da16-5770-9c03-1878f39d802d",
                "state": "active",
                "sport": "football",
                "translations": {
                    "en": [
                        {"type": "season", "value": "Liga 2 26/27"},
                        {"type": "team", "values": {"away": "Asa Targu Mures", "home": "AFC Metalul Buzau"}},
                    ]
                },
                "metadata": {"startedAt": "2026-10-10T08:00:00Z"},
                "closesAt": "2026-10-10T08:00:00Z",
            },
            "betting": {
                "hidden": False,
                "suspended": False,
                "markets": {
                    "winner3": {
                        "type": "winner3",
                        "hidden": False,
                        "open": True,
                        "options": {
                            "away": {"key": "away", "state": "active", "odds": 3.4},
                            "draw": {"key": "draw", "state": "active", "odds": 2.9},
                            "home": {"key": "home", "state": "active", "odds": 1.85},
                        },
                    },
                    "double-chance": {
                        "type": "double-chance",
                        "hidden": False,
                        "open": True,
                        "options": {
                            "home_or_draw": {"key": "home_or_draw", "state": "active", "odds": 1.22},
                        },
                    },
                },
            },
        }
    ],
    "count": 1,
    "categories": [],
    "tournaments": [
        {
            "uuid": "8debb4d7-da16-5770-9c03-1878f39d802d",
            "categoryUuid": "7b22f80b-2921-5af8-9b6b-195f7d975752",
            "slug": "liga-2",
            "translations": {"en": "Liga 2", "lt": "Lyga 2", "ru": "Liga 2"},
            "countryCode": "RO",
            "sport": "football",
        }
    ],
}


# Shape captured from a real, plain GET to the same endpoint with
# `filter.sports=rugby` (see scraper.py's module docstring for how that
# value was confirmed -- every other plausible slug 400s). Trimmed the
# same way as SAMPLE_RAW_PAYLOAD (dropped the player/competitor
# translation lists and every other market type), field names and values
# otherwise unchanged from the real response. Notably: rugby uses the
# exact same "winner3" three-way market as soccer (a rugby union match
# can end in a draw), not a separate two-way market -- confirmed by
# inspecting every fixture on a full live page (30/30 used "winner3").
RUGBY_SAMPLE_RAW_PAYLOAD = {
    "result": [
        {
            "fixture": {
                "uuid": "0d0891d0-23e0-507a-bb4b-92c3face022e",
                "type": "match",
                "categoryUuid": "ca88e612-92b1-5337-b367-723487eddcae",
                "tournamentUuid": "47b9a1e8-1eb5-531c-8042-b113f2eb319a",
                "state": "active",
                "sport": "rugby",
                "translations": {
                    "en": [
                        {"type": "season", "value": "Top 14 26/27"},
                        {"type": "team", "values": {"away": "Stade Francais Paris", "home": "Aviron Bayonne"}},
                    ]
                },
                "metadata": {"startedAt": "2026-10-03T14:35:00Z"},
                "closesAt": "2026-10-03T14:35:00Z",
            },
            "betting": {
                "hidden": False,
                "suspended": False,
                "markets": {
                    "winner3": {
                        "type": "winner3",
                        "hidden": False,
                        "open": True,
                        "options": {
                            "away": {"key": "away", "state": "active", "odds": 3.45},
                            "draw": {"key": "draw", "state": "active", "odds": 22},
                            "home": {"key": "home", "state": "active", "odds": 1.32},
                        },
                    },
                },
            },
        }
    ],
    "count": 1,
    "categories": [],
    "tournaments": [
        {
            "uuid": "47b9a1e8-1eb5-531c-8042-b113f2eb319a",
            "categoryUuid": "ca88e612-92b1-5337-b367-723487eddcae",
            "slug": "top-14",
            "translations": {"en": "Top 14", "lt": "Top 14", "ru": "Топ 14"},
            "countryCode": "FR",
            "sport": "rugby",
        }
    ],
}


def test_to_odds_events_maps_rugby_fixture_and_winner3_market():
    """Rugby goes through the exact same winner3 mapping path as soccer --
    it uses the same three-way market, just a much longer-shot draw
    price -- so this only needs to confirm the sport-name passthrough and
    that the mapping isn't accidentally soccer-specific."""
    scraper = EasybetScraper()

    events = scraper.to_odds_events(RUGBY_SAMPLE_RAW_PAYLOAD)

    assert len(events) == 1
    event = events[0]
    assert event.sport == "rugby"
    assert event.league == "Top 14"
    assert event.home_team == "Aviron Bayonne"
    assert event.away_team == "Stade Francais Paris"
    assert event.bookmaker == "easybet"
    assert event.event_id is None
    assert event.markets["moneyline"].home_odds == 1.32
    assert event.markets["moneyline"].away_odds == 3.45
    assert event.markets["moneyline"].draw_odds == 22
    assert list(event.markets.keys()) == ["moneyline"]


def test_to_odds_events_handles_soccer_and_rugby_in_the_same_batch_independently():
    """A single poll cycle now combines both sports' raw payloads (see
    poll()) before to_odds_events ever sees them combined -- but
    to_odds_events itself is also exercised directly here with a payload
    holding one of each, confirming the per-fixture sport lookup doesn't
    leak state between events of different sports in one batch."""
    payload = {
        "result": [SAMPLE_RAW_PAYLOAD["result"][0], RUGBY_SAMPLE_RAW_PAYLOAD["result"][0]],
        "tournaments": [*SAMPLE_RAW_PAYLOAD["tournaments"], *RUGBY_SAMPLE_RAW_PAYLOAD["tournaments"]],
    }
    scraper = EasybetScraper()

    events = scraper.to_odds_events(payload)

    assert len(events) == 2
    by_sport = {e.sport: e for e in events}
    assert by_sport["soccer"].home_team == "AFC Metalul Buzau"
    assert by_sport["rugby"].home_team == "Aviron Bayonne"


def test_to_odds_events_maps_fixture_and_winner3_market():
    scraper = EasybetScraper()

    events = scraper.to_odds_events(SAMPLE_RAW_PAYLOAD)

    assert len(events) == 1
    event = events[0]
    assert event.sport == "soccer"  # "football" normalized to match Betway ZA/WSB
    assert event.league == "Liga 2"
    assert event.home_team == "AFC Metalul Buzau"
    assert event.away_team == "Asa Targu Mures"
    assert event.bookmaker == "easybet"
    assert event.event_id is None  # left for the engine to compute
    assert event.markets["moneyline"].home_odds == 1.85
    assert event.markets["moneyline"].away_odds == 3.4
    assert event.markets["moneyline"].draw_odds == 2.9
    # only winner3 is mapped -- double-chance and every other market type
    # AdvBet exposes are out of scope for now.
    assert list(event.markets.keys()) == ["moneyline"]


def test_to_odds_events_skips_suspended_betting():
    payload = {
        **SAMPLE_RAW_PAYLOAD,
        "result": [
            {**SAMPLE_RAW_PAYLOAD["result"][0], "betting": {**SAMPLE_RAW_PAYLOAD["result"][0]["betting"], "suspended": True}}
        ],
    }
    scraper = EasybetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_hidden_betting():
    payload = {
        **SAMPLE_RAW_PAYLOAD,
        "result": [
            {**SAMPLE_RAW_PAYLOAD["result"][0], "betting": {**SAMPLE_RAW_PAYLOAD["result"][0]["betting"], "hidden": True}}
        ],
    }
    scraper = EasybetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_inactive_fixtures():
    fixture = {**SAMPLE_RAW_PAYLOAD["result"][0]["fixture"], "state": "removed"}
    payload = {**SAMPLE_RAW_PAYLOAD, "result": [{**SAMPLE_RAW_PAYLOAD["result"][0], "fixture": fixture}]}
    scraper = EasybetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_missing_winner3_market():
    betting = {**SAMPLE_RAW_PAYLOAD["result"][0]["betting"]}
    betting["markets"] = {k: v for k, v in betting["markets"].items() if k != "winner3"}
    payload = {**SAMPLE_RAW_PAYLOAD, "result": [{**SAMPLE_RAW_PAYLOAD["result"][0], "betting": betting}]}
    scraper = EasybetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_closed_winner3_market():
    event = SAMPLE_RAW_PAYLOAD["result"][0]
    markets = {**event["betting"]["markets"]}
    markets["winner3"] = {**markets["winner3"], "open": False}
    payload = {**SAMPLE_RAW_PAYLOAD, "result": [{**event, "betting": {**event["betting"], "markets": markets}}]}
    scraper = EasybetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_incomplete_price_data():
    """If a price is missing for home or away, don't publish a partial/misleading market."""
    event = SAMPLE_RAW_PAYLOAD["result"][0]
    markets = {**event["betting"]["markets"]}
    winner3 = {**markets["winner3"]}
    winner3["options"] = {"away": winner3["options"]["away"]}  # home odds missing
    markets["winner3"] = winner3
    payload = {**SAMPLE_RAW_PAYLOAD, "result": [{**event, "betting": {**event["betting"], "markets": markets}}]}
    scraper = EasybetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_unmapped_tournament_falls_back_to_unknown_league():
    payload = {**SAMPLE_RAW_PAYLOAD, "tournaments": []}
    scraper = EasybetScraper()

    events = scraper.to_odds_events(payload)

    assert len(events) == 1
    assert events[0].league == "unknown"


def test_to_odds_events_handles_multiple_events_independently():
    second = {
        "fixture": {
            "uuid": "aaaaaaaa-1111-2222-3333-444444444444",
            "type": "match",
            "categoryUuid": "cat-2",
            "tournamentUuid": "tourn-2",
            "state": "active",
            "sport": "football",
            "translations": {
                "en": [
                    {"type": "team", "values": {"away": "TeamB", "home": "TeamA"}},
                ]
            },
            "metadata": {"startedAt": "2026-11-01T15:00:00Z"},
            "closesAt": "2026-11-01T15:00:00Z",
        },
        "betting": {
            "hidden": False,
            "suspended": False,
            "markets": {
                "winner3": {
                    "type": "winner3",
                    "hidden": False,
                    "open": True,
                    "options": {
                        "away": {"key": "away", "state": "active", "odds": 2.10},
                        "draw": {"key": "draw", "state": "active", "odds": 3.20},
                        "home": {"key": "home", "state": "active", "odds": 3.40},
                    },
                }
            },
        },
    }
    payload = {**SAMPLE_RAW_PAYLOAD, "result": [SAMPLE_RAW_PAYLOAD["result"][0], second]}
    scraper = EasybetScraper()

    events = scraper.to_odds_events(payload)

    assert len(events) == 2
    assert {e.home_team for e in events} == {"AFC Metalul Buzau", "TeamA"}


def test_to_odds_events_skips_a_malformed_event_without_crashing_the_batch():
    """One event missing a required field (e.g. team translations) must not
    take down parsing of every other event in the same bulk payload."""
    malformed = {
        "fixture": {
            "uuid": "bad-event",
            "type": "match",
            "categoryUuid": "cat-x",
            "tournamentUuid": "tourn-x",
            "state": "active",
            "sport": "football",
            "translations": {"en": []},  # no "team" entry
            "metadata": {"startedAt": "2026-11-01T15:00:00Z"},
        },
        "betting": {
            "hidden": False,
            "suspended": False,
            "markets": {
                "winner3": {
                    "type": "winner3",
                    "hidden": False,
                    "open": True,
                    "options": {
                        "away": {"key": "away", "state": "active", "odds": 2.10},
                        "home": {"key": "home", "state": "active", "odds": 3.40},
                    },
                }
            },
        },
    }
    payload = {**SAMPLE_RAW_PAYLOAD, "result": [SAMPLE_RAW_PAYLOAD["result"][0], malformed]}
    scraper = EasybetScraper()

    events = scraper.to_odds_events(payload)

    # The well-formed event still comes through; the malformed one is
    # skipped rather than raising and losing the whole batch.
    assert len(events) == 1
    assert events[0].home_team == "AFC Metalul Buzau"


@pytest.mark.asyncio
async def test_poll_yields_events_on_a_fixed_interval(monkeypatch):
    """poll() now fetches every sport in scraper.sports (football and
    rugby by default) each cycle and yields one combined list -- the fake
    below returns each sport's own sample payload, so a full cycle's batch
    has one event per sport."""
    scraper = EasybetScraper()
    payloads = {"football": SAMPLE_RAW_PAYLOAD, "rugby": RUGBY_SAMPLE_RAW_PAYLOAD}

    async def fake_fetch_raw_odds(sport):
        return payloads[sport]

    monkeypatch.setattr(scraper, "fetch_raw_odds", fake_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        if len(results) == 3:
            break

    assert len(results) == 3
    for batch in results:
        assert len(batch) == 2
        assert {e.home_team for e in batch} == {"AFC Metalul Buzau", "Aviron Bayonne"}


@pytest.mark.asyncio
async def test_poll_continues_past_a_transient_fetch_failure_for_one_sport(monkeypatch):
    """If only one sport's fetch fails this cycle (e.g. a momentary 5xx),
    the other sport's odds still get yielded rather than withholding the
    whole cycle -- same per-record defensiveness to_odds_events already
    applies to a single malformed event, applied here per-sport."""
    import httpx

    scraper = EasybetScraper()

    async def flaky_fetch_raw_odds(sport):
        if sport == "football":
            raise httpx.ConnectError("simulated network blip")
        return RUGBY_SAMPLE_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", flaky_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        break

    assert len(results) == 1
    assert len(results[0]) == 1
    assert results[0][0].home_team == "Aviron Bayonne"


@pytest.mark.asyncio
async def test_poll_yields_nothing_when_every_sport_fetch_fails(monkeypatch):
    """A total outage (every sport's fetch fails) stays silent for the
    cycle -- exactly like the pre-rugby single-sport version did -- so the
    caller's heartbeat write is skipped too and a real outage still shows
    up as an expired heartbeat key downstream rather than a healthy
    "0 events" cycle. Since poll() never yields in this scenario, an
    ordinary `async for` would hang forever (bounded only by pytest's
    global timeout) -- pull from the generator directly under a short
    asyncio.wait_for instead, so a correct implementation (no yield) is
    what makes this test pass quickly, not what makes it hang.
    """
    import httpx

    scraper = EasybetScraper()
    call_count = 0

    async def always_failing_fetch_raw_odds(sport):
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("simulated total outage")

    monkeypatch.setattr(scraper, "fetch_raw_odds", always_failing_fetch_raw_odds)

    generator = scraper.poll(interval_seconds=0.01)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(generator.__anext__(), timeout=0.5)

    assert call_count >= 2  # at least one attempt per sport, across at least one full cycle


@pytest.mark.asyncio
async def test_poll_recovers_after_a_fully_failed_cycle(monkeypatch):
    import httpx

    scraper = EasybetScraper()
    cycle = 0

    async def fetch_raw_odds(sport):
        nonlocal cycle
        # First cycle (both sports) fails outright; second cycle succeeds.
        if cycle < 2:
            cycle += 1
            raise httpx.ConnectError("simulated network blip")
        return SAMPLE_RAW_PAYLOAD if sport == "football" else RUGBY_SAMPLE_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        break

    assert len(results) == 1
    assert len(results[0]) == 2
