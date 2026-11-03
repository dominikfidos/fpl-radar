"""Warm the cache and check that the report can be built.

Run with:  python -m app.league.refresh
In Kubernetes this is the command of a CronJob (same image as the API).
Needs the environment variables FPL_LEAGUE_ID and FPL_ENTRY_ID.
"""

import logging
import os
import sys

from app.database import SessionLocal
from app.league.cache import DbCache
from app.league.client import FplClient
from app.league.history import record_rank_history
from app.league.service import build_report
from app.migrate import migrate_on_startup

log = logging.getLogger("league-refresh")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        league_id = int(os.environ["FPL_LEAGUE_ID"])
        entry_id = int(os.environ["FPL_ENTRY_ID"])
    except (KeyError, ValueError):
        log.error("Set FPL_LEAGUE_ID and FPL_ENTRY_ID to numbers.")
        return 2

    migrate_on_startup()
    client = FplClient(cache=DbCache(SessionLocal))
    try:
        report = build_report(client, league_id, entry_id)
        written = record_rank_history(SessionLocal, client, league_id, entry_id)
        log.info("Rank history: %s rows stored (0 = already up to date)", written)
    finally:
        client.close()

    meta = report["meta"]
    log.info(
        "Report ready: rank %s, %s rivals analysed, %s suggestions (squads as of GW%s)",
        meta["my_rank"],
        len(meta["rivals"]),
        len(report["suggestions"]),
        meta["squad_as_of_gw"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
