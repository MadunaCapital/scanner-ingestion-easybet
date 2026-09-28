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
    scraper = EasybetScraper()

    async def fake_fetch_raw_odds():
        return SAMPLE_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", fake_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        if len(results) == 3:
            break

    assert len(results) == 3
    assert all(len(batch) == 1 and batch[0].home_team == "AFC Metalul Buzau" for batch in results)


@pytest.mark.asyncio
async def test_poll_continues_past_a_transient_fetch_failure(monkeypatch):
    import httpx

    scraper = EasybetScraper()
    call_count = 0

    async def flaky_fetch_raw_odds():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("simulated network blip")
        return SAMPLE_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", flaky_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        break  # first successful yield should be the second call, after the failure

    assert call_count == 2
    assert len(results) == 1
    assert results[0][0].home_team == "AFC Metalul Buzau"
