import argparse
import json
import sys

from app.agent_runs import (
    WORKFLOW_LEAD_SYNTHESIS_V1,
    WORKFLOW_OWNERSHIP_V1,
    enqueue_agent_run,
    get_agent_run,
)
from app.config import get_settings
from app.db import ensure_schema, get_connection
from app.ingest import ingest_newark
from app.ownership import enrich_newark_ownership
from app.tasks import run_agent_run, run_ownership_agent_run


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _wait_for_run_payload(database_url, run_id, timeout_seconds=300):
    import time

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        connection = get_connection(database_url)
        try:
            run = get_agent_run(connection, run_id)
        finally:
            connection.close()
        if run and run["status"] in {"completed", "failed"}:
            return run
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for agent run %s" % run_id)


def main():
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Newark real-data foundation CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest-newark", help="Download and ingest Newark foundation data")
    ingest_parser.add_argument(
        "--fixtures-dir",
        help="Use local fixture data instead of downloading live sources",
    )
    ingest_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Run without progress logs",
    )
    ingest_parser.add_argument(
        "--allow-fixture-reset",
        action="store_true",
        help="Allow fixture runs to replace an existing Newark dataset in the current database",
    )
    ownership_parser = subparsers.add_parser(
        "enrich-newark-ownership",
        help="Enrich Newark parcels with owner-of-record and NJ entity summary data",
    )
    ownership_parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Number of Tier 1 Newark parcels to enrich in batch mode; omit to target all Tier 1 parcels",
    )
    ownership_parser.add_argument(
        "--parcel-id",
        help="Enrich a single Newark parcel by id instead of the default top Tier 1 batch",
    )
    ownership_parser.add_argument(
        "--fixtures-dir",
        help="Use local fixture HTML instead of live source requests",
    )
    ownership_parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore cached ownership HTML and re-fetch live sources",
    )
    ownership_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Run without progress logs",
    )
    ownership_parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for queued ownership jobs to finish",
    )
    lead_parser = subparsers.add_parser(
        "enrich-newark-leads",
        help="Enrich Newark with recruiter-grade lead candidates",
    )
    lead_parser.add_argument(
        "--top-n",
        type=int,
        default=75,
        help="Maximum ownership-derived seed people to consider before civic/profile sources are added",
    )
    lead_parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the queued lead generation run to finish",
    )

    args = parser.parse_args()
    if args.command == "ingest-newark":
        result = ingest_newark(
            fixtures_dir=args.fixtures_dir,
            progress=None if args.quiet else _log,
            allow_fixture_reset=args.allow_fixture_reset,
        )
    elif args.command == "enrich-newark-ownership":
        if args.fixtures_dir or args.force_refresh:
            result = enrich_newark_ownership(
                top_n=args.top_n,
                parcel_id=args.parcel_id,
                fixtures_dir=args.fixtures_dir,
                force_refresh=args.force_refresh,
                progress=None if args.quiet else _log,
            )
        else:
            connection = get_connection(settings.database_url)
            queued_runs = []
            try:
                ensure_schema(connection)
                with connection.cursor() as cursor:
                    if args.parcel_id:
                        cursor.execute(
                            "SELECT id FROM parcels WHERE market_id = %s AND id = %s",
                            (settings.market_id, args.parcel_id),
                        )
                    else:
                        if args.top_n is None:
                            cursor.execute(
                                """
                                SELECT id
                                FROM parcels
                                WHERE market_id = %s
                                  AND priority_tier = 'Tier 1'
                                ORDER BY priority_score DESC NULLS LAST, total_assessed_value DESC, id
                                """,
                                (settings.market_id,),
                            )
                        else:
                            cursor.execute(
                                """
                                SELECT id
                                FROM parcels
                                WHERE market_id = %s
                                  AND priority_tier = 'Tier 1'
                                ORDER BY priority_score DESC NULLS LAST, total_assessed_value DESC, id
                                LIMIT %s
                                """,
                                (settings.market_id, args.top_n),
                            )
                    parcel_ids = [row["id"] for row in cursor.fetchall()]

                _log("Ownership target parcels: %s" % len(parcel_ids))
                for parcel_id in parcel_ids:
                    queued = enqueue_agent_run(
                        connection,
                        settings.market_id,
                        "parcel",
                        parcel_id,
                        WORKFLOW_OWNERSHIP_V1,
                        {
                            "market_id": settings.market_id,
                            "parcel_id": parcel_id,
                        },
                    )
                    if queued["created"]:
                        run_ownership_agent_run.delay(queued["run_id"])
                    queued_runs.append(
                        {
                            "parcel_id": parcel_id,
                            "agent_run_id": queued["run_id"],
                            "created": queued["created"],
                        }
                    )
                result = {
                    "market_id": settings.market_id,
                    "parcel_count": len(parcel_ids),
                    "queued_runs": queued_runs,
                }
            finally:
                connection.close()

            if args.wait and queued_runs:
                _log("Waiting for ownership runs to finish")
                completed_runs = []
                for queued in queued_runs:
                    run_payload = _wait_for_run_payload(settings.database_url, queued["agent_run_id"])
                    completed_runs.append(run_payload)
                result["runs"] = completed_runs
    elif args.command == "enrich-newark-leads":
        connection = get_connection(settings.database_url)
        try:
            ensure_schema(connection)
            queued = enqueue_agent_run(
                connection,
                settings.market_id,
                "market",
                settings.market_id,
                WORKFLOW_LEAD_SYNTHESIS_V1,
                {
                    "market_id": settings.market_id,
                    "top_n": args.top_n,
                    "source_families": ["ownership_officer", "civic_roster"],
                    "promotion_state": "lead_generation",
                    "persona": "lead",
                },
            )
        finally:
            connection.close()
        if queued["created"]:
            run_agent_run.delay(queued["run_id"])
        result = {
            "market_id": settings.market_id,
            "agent_run_id": queued["run_id"],
            "created": queued["created"],
        }
        if args.wait:
            _log("Waiting for lead generation run to finish")
            result["run"] = _wait_for_run_payload(settings.database_url, queued["run_id"])
    else:
        raise SystemExit("Unknown command")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
