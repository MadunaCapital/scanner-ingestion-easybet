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
# other bookmakers). "winner3" is the standard three-way 1X2/moneyline
# market (home/draw/away); AdvBet exposes hundreds of other market types
# (totals, handicaps, BTTS, etc.) per event, out of scope for now, same
# as Betway ZA and WSB only handling their moneyline-equivalent market.
MARKET_TYPE_MAP = {
    "winner3": "moneyline",
}

# AdvBet's sport slug -> universal sport name used by the other bookmaker
# adapters in this project (both Betway ZA and WSB report "soccer").
SPORT_NAME_MAP = {
    "football": "soccer",
}


class EasybetScraper(BaseScraper):
    bookmaker_id = "easybet"

    def __init__(self, sport: str = "football", limit: int = MAX_LIMIT):
        self.sport = sport
        self.limit = limit
        self._client = httpx.AsyncClient(timeout=15)

    async def fetch_raw_odds(self) -> dict:
        response = await self._client.get(
            EASYBET_EVENTS_URL,
            params={
                "view": "full",
                "filter.types": "match",
                "filter.sports": self.sport,
                "cursor.limit": self.limit,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()

    def to_odds_events(self, raw: dict) -> list[OddsEvent]:
        """Maps AdvBet's fixture/betting event shape onto the universal
        OddsEvent schema. Moneyline (winner3) market only for now.

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

                market = betting.get("markets", {}).get("winner3")
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

                sport = SPORT_NAME_MAP.get(fixture.get("sport"), fixture.get("sport", self.sport))

                odds_events.append(
                    OddsEvent(
                        sport=sport,
                        league=league,
                        home_team=home_team,
                        away_team=away_team,
                        start_time=start_time,
                        bookmaker=self.bookmaker_id,
                        markets={
                            MARKET_TYPE_MAP["winner3"]: MarketOdds(
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
        """Fetches odds on a fixed interval and yields the parsed events each
        time. A transient fetch failure (network blip, momentary 5xx, or a
        non-JSON error page served with a 200 status) is logged and the loop
        continues on schedule rather than crashing -- this loop is meant to
        run unattended for the life of the process, so nothing from a single
        bad cycle should ever be allowed to kill it. The freshness circuit
        breaker downstream is what protects against acting on odds that are
        actually stale, not this loop.
        """
        while True:
            try:
                raw = await self.fetch_raw_odds()
                yield self.to_odds_events(raw)
            except httpx.HTTPError as exc:
                logger.warning("%s: poll fetch failed: %s", self.bookmaker_id, exc)
            except Exception:
                logger.exception("%s: unexpected error in poll cycle", self.bookmaker_id)

            await asyncio.sleep(interval_seconds)

    async def close(self) -> None:
        await self._client.aclose()
