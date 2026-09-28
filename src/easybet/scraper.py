"""Easybet (easybet.co.za) adapter.

Reads the public, unauthenticated odds feed of the third-party sportsbook
platform ("AdvBet") that powers Easybet's `/sports` section. Plain HTTP
GET with no TLS impersonation, no stealth browser, and no Cloudflare
bypass: easybet.co.za itself sits behind CloudFront/nginx (not
Cloudflare), and the actual odds API below has no bot-detection layer --
it just validates ordinary query parameters.

Endpoint discovery chain (all plain GET/POST, no auth, done once by hand
to find the static identifiers baked in below -- none of this is repeated
at scrape time):

1. `GET https://easybet.co.za/config.json` -- public site config,
   confirms `websiteID` 4000169 and points at the "Aardvark" gaming
   platform's backend.
2. `GET https://<aardvark-backend>/v1/web-backend/game` with header
   `X-Website-ID: 4000169` -- public per-website game catalog. The
   `game-aggregator-sportsbook` entry (gameID 4301) carries a static
   `operatorUUID` ("0191fb10-7058-7104-aac5-02b11bbebf23") identifying
   Easybet's organization on the underlying AdvBet sportsbook platform.
3. `POST .../public-external-game/4301/enter/` (anonymous/public flow,
   no login) returns a gateway URL, which redirects to
   `https://sa01.sportsbook.adv.bet/?orgUuid=<operatorUUID>` -- the
   sportsbook single-page app Easybet embeds in an iframe.
4. That app's own bundle shows it talks directly to
   `https://sportsbook-sa01-backend.advbet.com/distributor/api/organizations/<operatorUUID>`
   for event/odds data -- a plain REST GET, confirmed working below with
   zero authentication.

Because `operatorUUID` is a fixed identifier for Easybet's organization
(not a session token -- it doesn't expire or rotate per request), it's
hardcoded as EASYBET_ORG_UUID rather than rediscovered on every poll.

Sports covered: soccer (`filter.sports=football`), rugby
(`filter.sports=rugby`), cricket (`filter.sports=cricket`) and tennis
(`filter.sports=tennis`) -- South Africa's #1-#3 sports by popularity
plus tennis. The rugby, cricket and tennis values were all found the
same way as `football`: a plain GET to the events endpoint with the
candidate slug. `rugby` returned HTTP 200 with real fixtures
(`fixture.sport == "rugby"`, e.g. Top 14 matches) on the first try;
other plausible slugs (`rugby-union`, `rugby_union`, `rugbyunion`,
`rugby-league`) all 400 with the API's own "invalid sport <value>"
schema error, confirming `rugby` is the one real value. `cricket` also
returned HTTP 200 directly on the first try (46 live fixtures, e.g.
CSA T20 Challenge and various international ODI series), so no
alternate slug needed to be tried. `tennis` likewise returned HTTP 200
directly on the first try (a full 100-event page, e.g. ATP Beijing
singles matches), no alternate slug needed either.

Market shape differs by sport, though. Every rugby fixture inspected
(30 live events, one full page) uses the exact same `winner3` three-way
1X2 market as soccer -- rugby union matches can end in a draw, so
AdvBet doesn't special-case a two-way market for it. Cricket does NOT
follow that pattern: no cricket fixture in a full live page (46 events)
carried a plain `winner3` market at all. Instead:

- Limited-overs cricket (T20, ODI, T10 -- 44 of 46 fixtures inspected,
  covering domestic T20 leagues and ODI series) uses `winner2-incl-overtime`,
  a genuinely two-way market (`options` only has `home`/`away` keys, no
  `draw` key present at all -- there's no rain-out/tie provision modeled
  here). This is the moneyline-equivalent market mapped below, the same
  role `winner3` plays for soccer/rugby.
- Multi-day cricket (first-class/Test-length matches -- the only 2 of
  46 fixtures inspected, "Presidents Trophy" and a first-class India A
  vs Australia A series) instead carries `winner-draw-no-bet` and no
  `winner2-incl-overtime` at all. That's a genuinely different betting
  product (stake refunded on a draw, not a priced third outcome) rather
  than a same-shape three-way market like rugby's, so it's deliberately
  left unmapped here -- same as `double-chance` and every other
  non-moneyline market type, out of scope for now rather than worked
  around. This means Test/first-class cricket fixtures are silently
  skipped by to_odds_events (no known moneyline-shaped market found for
  them), the same code path already used for any event whose only
  markets aren't in MONEYLINE_MARKET_TYPES.

Tennis needed yet another market-type key. A tennis match can't end in
a draw (no overtime concept either -- it's played to a set/game
conclusion), so a two-way market is expected, but it's neither of the
two-way keys already mapped: every fixture on a full live page (100/100
inspected, e.g. ATP Beijing singles) carries a market literally called
`winner2` (`options` has only `home`/`away`, no `draw`) -- not
`winner2-incl-overtime`, which cricket uses. No fixture in that page
carried `winner2-incl-overtime` or `winner3` at all, so `winner2` is
tennis's own distinct moneyline-equivalent key, mapped as a new
MARKET_TYPE_MAP/MONEYLINE_MARKET_TYPES entry rather than reusing
cricket's.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
from ingestion.base_scraper import BaseScraper
from schemas import MarketOdds, OddsEvent

logger = logging.getLogger(__name__)

# Easybet's static organization identifier on the AdvBet sportsbook
# platform (adv.bet / advbet.com). See the module docstring for how this
# was discovered -- it is not a session token and does not need refreshing.
EASYBET_ORG_UUID = "0191fb10-7058-7104-aac5-02b11bbebf23"

EASYBET_EVENTS_URL = (
    f"https://sportsbook-sa01-backend.advbet.com/distributor/api/organizations/{EASYBET_ORG_UUID}/events"
)

# The API rejects any cursor.limit above 100 with a 400 ("invalid limit"),
# discovered empirically -- this is the max page size in one plain GET.
MAX_LIMIT = 100

# Plain, fixed-interval polling -- same cadence as a normal page refresh,
# not randomized or disguised to look human. See the README's scope note.
DEFAULT_POLL_INTERVAL_SECONDS = 45

# AdvBet's market type key -> universal market key, per the plan's
# market-mapping approach (scanner-engine/formatting.py does the same for
# other bookmakers). AdvBet exposes hundreds of other market types
# (totals, handicaps, BTTS, etc.) per event, out of scope for now, same
# as Betway ZA and WSB only handling their moneyline-equivalent market.
#
# "winner3" is the standard three-way 1X2/moneyline market (home/draw/away)
# soccer and rugby both use -- a rugby union match can end in a draw, so
# AdvBet models it with the same three-way options soccer uses, just a
# much longer-shot draw price. No rugby-specific market key was found in
# any live fixture inspected.
#
# Cricket does not use "winner3" at all (see the module docstring) --
# limited-overs cricket's moneyline-equivalent is the two-way
# "winner2-incl-overtime" market instead (no draw key in its options).
# Tennis uses yet another two-way key, plain "winner2" (no
# "-incl-overtime" suffix -- tennis has no overtime concept). All three
# map to the same universal "moneyline" key; which one a given fixture
# actually carries is resolved by MONEYLINE_MARKET_TYPES below.
MARKET_TYPE_MAP = {
    "winner3": "moneyline",
    "winner2-incl-overtime": "moneyline",
    "winner2": "moneyline",
}

# The AdvBet market type keys that represent a "moneyline" market, in the
# order to check for on a given fixture -- a fixture is expected to carry
# at most one of these. "winner3" first since that's the three-way market
# soccer/rugby (and, hypothetically, a future 3-way cricket format) use;
# "winner2-incl-overtime" is cricket's own two-way equivalent; "winner2"
# is tennis's own two-way equivalent (no "-incl-overtime" suffix -- see
# the module docstring). A fixture with none of these (e.g.
# multi-day/Test-length cricket, which instead carries
# "winner-draw-no-bet" -- a different betting product, not a same-shape
# market, see the module docstring) is skipped, same as any other event
# missing a moneyline-shaped market.
MONEYLINE_MARKET_TYPES: tuple[str, ...] = ("winner3", "winner2-incl-overtime", "winner2")

# AdvBet's sport slug -> universal sport name used by the other bookmaker
# adapters in this project (both Betway ZA and WSB report "soccer").
# "rugby", "cricket" and "tennis" are left as identity mappings (AdvBet's
# own slugs already match the universal names we want) but spelled out
# explicitly rather than relying on the fallback, in case AdvBet ever
# splits any of them into more specific slugs (union/league/sevens for
# rugby, T20/ODI/Test for cricket, singles/doubles for tennis).
SPORT_NAME_MAP = {
    "football": "soccer",
    "rugby": "rugby",
    "cricket": "cricket",
    "tennis": "tennis",
}

# The sports this adapter polls every cycle. Each is fetched with its own
# plain GET (same endpoint, different `filter.sports` value) rather than
# one combined request: AdvBet does accept a repeated `filter.sports`
# query param to OR multiple sports together, but the result is a single
# page of `cursor.limit` (max 100) events ranked with no per-sport
# guarantee -- with ~1100+ live football fixtures against ~30 rugby ones,
# a combined request's first page is almost entirely football and starves
# the smaller sports out. Separate per-sport requests, each within its
# own 100-event page, is what actually gets every sport's data -- cricket
# (46 fixtures in its own full page) and tennis (a full 100-event page on
# its own) follow the same reasoning.
EASYBET_SPORTS: tuple[str, ...] = ("football", "rugby", "cricket", "tennis")


class EasybetScraper(BaseScraper):
    bookmaker_id = "easybet"

    def __init__(self, sports: tuple[str, ...] = EASYBET_SPORTS, limit: int = MAX_LIMIT):
        self.sports = tuple(sports)
        self.limit = limit
        self._client = httpx.AsyncClient(timeout=15)

    async def fetch_raw_odds(self, sport: str) -> dict:
        response = await self._client.get(
            EASYBET_EVENTS_URL,
            params={
                "view": "full",
                "filter.types": "match",
                "filter.sports": sport,
                "cursor.limit": self.limit,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()

    def to_odds_events(self, raw: dict) -> list[OddsEvent]:
        """Maps AdvBet's fixture/betting event shape onto the universal
        OddsEvent schema. Moneyline market only for now -- resolved via
        MONEYLINE_MARKET_TYPES since the actual market type key differs by
        sport ("winner3" for soccer/rugby, "winner2-incl-overtime" for
        limited-overs cricket, "winner2" for tennis; see the module
        docstring). Sport agnostic -- it reads each fixture's own `sport`
        field (via SPORT_NAME_MAP) rather than trusting which
        `self.sports` entry the caller happened to fetch, so it works the
        same for a football, rugby, cricket or tennis raw payload.

        The response's top-level `tournaments` array is a lookup table
        (by uuid, not filtered to only the events in this page) used to
        resolve each fixture's `tournamentUuid` to a human-readable
        league name -- joined here the same way Betway ZA and WSB join
        their own bulk arrays.

        Note event_id (the OddsEvent field) is left unset here -- that's
        the engine's job downstream (see the note on OddsEvent.event_id
        in scanner-schemas).
        """
        scraped_at = datetime.now(timezone.utc)

        tournament_by_uuid: dict[str, dict] = {}
        for tournament in raw.get("tournaments", []):
            try:
                tournament_by_uuid[tournament["uuid"]] = tournament
            except (KeyError, TypeError):
                logger.warning("Skipping malformed tournament entry: %r", tournament)

        odds_events: list[OddsEvent] = []

        # Every record below is handled defensively against a single
        # malformed entry (missing/odd field shape): skip and log just
        # that record rather than let one bad entry in a ~100-item bulk
        # payload crash the whole poll cycle.
        for item in raw.get("result", []):
            try:
                fixture = item.get("fixture", {})
                betting = item.get("betting", {})

                if fixture.get("state") != "active":
                    continue
                if betting.get("hidden") or betting.get("suspended"):
                    continue

                markets_raw = betting.get("markets", {})
                market_type = next((mt for mt in MONEYLINE_MARKET_TYPES if mt in markets_raw), None)
                market = markets_raw.get(market_type) if market_type is not None else None
                if market is None or market.get("hidden") or not market.get("open"):
                    continue

                options = market.get("options", {})
                home_odds = away_odds = draw_odds = None
                for key, target in (("home", "home"), ("away", "away"), ("draw", "draw")):
                    option = options.get(key)
                    if option is None or option.get("state") != "active":
                        continue
                    odds_value = option.get("odds")
                    if target == "home":
                        home_odds = odds_value
                    elif target == "away":
                        away_odds = odds_value
                    else:
                        draw_odds = odds_value

                if home_odds is None or away_odds is None:
                    continue  # incomplete market, don't publish a partial price

                team_translations = next(
                    (t for t in fixture.get("translations", {}).get("en", []) if t.get("type") == "team"),
                    None,
                )
                if team_translations is None:
                    continue
                home_team = team_translations.get("values", {}).get("home")
                away_team = team_translations.get("values", {}).get("away")
                if not home_team or not away_team:
                    continue

                start_time_raw = fixture.get("metadata", {}).get("startedAt") or fixture.get("closesAt")
                if not start_time_raw:
                    continue
                start_time = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))

                tournament = tournament_by_uuid.get(fixture.get("tournamentUuid"), {})
                league = tournament.get("translations", {}).get("en", "unknown")

                sport = SPORT_NAME_MAP.get(fixture.get("sport"), fixture.get("sport", "unknown"))

                odds_events.append(
                    OddsEvent(
                        sport=sport,
                        league=league,
                        home_team=home_team,
                        away_team=away_team,
                        start_time=start_time,
                        bookmaker=self.bookmaker_id,
                        markets={
                            MARKET_TYPE_MAP[market_type]: MarketOdds(
                                home_odds=home_odds, away_odds=away_odds, draw_odds=draw_odds
                            )
                        },
                        scraped_at=scraped_at,
                    )
                )
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.warning("Skipping malformed event: %s", exc)
                continue

        return odds_events

    async def poll(
        self, interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    ) -> AsyncIterator[list[OddsEvent]]:
        """Fetches odds for every sport in self.sports on a fixed interval
        and yields one combined list of parsed events per cycle. A
        transient fetch failure for one sport (network blip, momentary
        5xx, or a non-JSON error page served with a 200 status) is logged
        and only that sport is skipped for this cycle -- same "one bad
        record doesn't take down the batch" defensiveness as
        to_odds_events applies per-event, just applied per-sport here so a
        rugby-only outage (say) doesn't also withhold that cycle's
        perfectly good soccer odds. This loop is meant to run unattended
        for the life of the process, so nothing from a single bad cycle
        should ever be allowed to kill it. The freshness circuit breaker
        downstream is what protects against acting on odds that are
        actually stale, not this loop.

        Only yields when at least one sport's fetch actually succeeded this
        cycle -- if every sport fails (not just one), this stays silent for
        the cycle exactly like the single-sport version used to, so the
        caller's heartbeat write is skipped too and a real total outage
        still shows up as an expired heartbeat key downstream rather than
        being masked as a healthy "0 events" cycle.
        """
        while True:
            cycle_events: list[OddsEvent] = []
            any_fetch_succeeded = False
            for sport in self.sports:
                try:
                    raw = await self.fetch_raw_odds(sport)
                    any_fetch_succeeded = True
                    cycle_events.extend(self.to_odds_events(raw))
                except httpx.HTTPError as exc:
                    logger.warning("%s: poll fetch failed for sport=%s: %s", self.bookmaker_id, sport, exc)
                except Exception:
                    logger.exception("%s: unexpected error in poll cycle for sport=%s", self.bookmaker_id, sport)

            if any_fetch_succeeded:
                yield cycle_events

            await asyncio.sleep(interval_seconds)

    async def close(self) -> None:
        await self._client.aclose()
