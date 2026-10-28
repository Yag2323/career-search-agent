from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from application_capability import enrich_application_capability
from career_agent_common import JOBS_DIR, STATE_DIR, clean
from career_store import CareerStore
from career_targeting import (
    career_stage_rank,
    is_target_degree_role,
    posting_age_rank,
    recent_enough_for_digest,
    recency_rank,
)
from job_identity import enrich_known_portal_metadata, is_specific_job_listing, stabilize_and_merge_jobs
from job_delivery import filter_undelivered, was_delivered
from job_quality import score_job

ATS_JSON = JOBS_DIR / "current_ats_jobs.json"
ATS_CSV = JOBS_DIR / "current_ats_jobs.csv"
ATS_MD = JOBS_DIR / "current_ats_jobs.md"
ATS_STATUS = STATE_DIR / "current_ats_search_status.json"
RAPID_STATE = STATE_DIR / "rapid_job_watch_state.json"
RAPID_STATUS = STATE_DIR / "rapid_job_watch_status.json"
LATEST_JSON = JOBS_DIR / "latest_jobs.json"
CHECKED_JSON = JOBS_DIR / "latest_jobs_checked.json"
WEB_STATUS = JOBS_DIR / "availability_search_status.json"

BOARD_COMPANIES = {
    "akunacapital": "Akuna Capital",
    "dforeferrals": "Da Vinci",
    "drweng": "DRW",
    "goventi": "Venti Technologies",
    "wehrtyou": "Hudson River Trading",
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        fields = ["job_id", "company", "title", "location", "link", "availability_status"]
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def active_source_keys() -> tuple[set[str], str]:
    status = load_json(RAPID_STATUS, {})
    state = load_json(RAPID_STATE, {})
    stamp = clean(status.get("started_at"))
    if clean(status.get("status")).upper() != "SUCCESS" or not stamp:
        return set(), stamp
    sources = state.get("sources") if isinstance(state, dict) else {}
    boards = state.get("boards") if isinstance(state, dict) else {}
    unchanged_boards = {
        clean(key).lower()
        for key, value in (boards or {}).items()
        if isinstance(value, dict)
        and clean(value.get("last_checked_at")) == stamp
        and clean(value.get("last_status")).upper() == "NOT_MODIFIED"
    }
    return {
        clean(key)
        for key, value in (sources or {}).items()
        if isinstance(value, dict)
        and (
            clean(value.get("last_seen")) == stamp
            or f"{clean(value.get('provider')).upper()}:{clean(value.get('board')).lower()}".lower()
            in unchanged_boards
        )
    }, stamp


def rank_key(row: dict[str, Any]) -> tuple[int, float, int, int, int, str]:
    return (
        recency_rank(row),
        posting_age_rank(row),
        career_stage_rank(row),
        int(float(clean(row.get("degree_fit_score")) or 0)),
        int(float(clean(row.get("quality_score")) or 0)),
        clean(row.get("job_id")),
    )


def current_ats_rows() -> tuple[list[dict[str, Any]], str]:
    active_keys, stamp = active_source_keys()
    if not active_keys:
        return [], stamp
    rows: list[dict[str, Any]] = []
    for stored in CareerStore().list_jobs():
        if "RAPID_DIRECT_ATS_WATCH" not in clean(stored.get("source_query")).upper():
            continue
        if clean(stored.get("source_key")) not in active_keys:
            continue
        row = dict(stored)
        if not is_specific_job_listing(clean(row.get("title")), clean(row.get("canonical_url") or row.get("link"))):
            continue
        board = clean(row.get("ats_board")).lower()
        if board in BOARD_COMPANIES:
            row["company"] = BOARD_COMPANIES[board]
        row = score_job(enrich_application_capability(enrich_known_portal_metadata(row)))
        if clean(row.get("availability_status")).upper() != "OPEN":
            continue
        if clean(row.get("apply_type")).upper() == "SKIP":
            continue
        if not is_target_degree_role(row)[0] or not recent_enough_for_digest(row):
            continue
        rows.append(row)
    rows, _aliases = stabilize_and_merge_jobs(rows, preserve_legacy_aliases=False)
    rows = filter_undelivered(rows)
    rows.sort(key=rank_key, reverse=True)
    return rows, stamp


def merge_web_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status = load_json(WEB_STATUS, {})
    search_run_id = clean(status.get("search_run_id"))
    checked_rows = load_json(CHECKED_JSON, [])
    candidates = (
        list(checked_rows)
        if clean(status.get("status")).upper() == "SUCCESS"
        and search_run_id
        and isinstance(checked_rows, list)
        else []
    )

    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        if clean(raw.get("search_run_id")) != search_run_id:
            continue
        if was_delivered(raw):
            continue
        if not is_specific_job_listing(clean(raw.get("title")), clean(raw.get("canonical_url") or raw.get("link"))):
            continue
        identity = clean(raw.get("job_id")) or clean(raw.get("canonical_url") or raw.get("link"))
        if not identity or identity in seen:
            continue
        seen.add(identity)
        row = score_job(raw)
        if clean(row.get("availability_status")).upper() != "OPEN":
            continue
        if clean(row.get("apply_type")).upper() == "SKIP":
            continue
        if is_target_degree_role(row)[0] and recent_enough_for_digest(row):
            rows.append(row)
    rows, _aliases = stabilize_and_merge_jobs(rows, preserve_legacy_aliases=False)
    rows = filter_undelivered(rows)
    rows.sort(key=rank_key, reverse=True)
    return rows


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Current Direct ATS Jobs",
        "",
        f"Generated: {datetime.now().astimezone().isoformat(timespec='minutes')}",
        "",
        "Only unseen Singapore vacancies still present in a public employer ATS feed and within the supported freshness window are included.",
        "Published and updated timestamps are labelled separately.",
        "",
    ]
    if not rows:
        lines.append("No qualifying current ATS jobs were found.")
    for row in rows:
        lines.extend(
            [
                f"## {clean(row.get('job_id'))} - {clean(row.get('title'))}",
                "",
                f"- Company: {clean(row.get('company'))}",
                f"- Career stage: {clean(row.get('career_stage'))}",
                f"- Degree fit: {clean(row.get('degree_fit_score'))}/100",
                f"- Quality score: {clean(row.get('quality_score'))}",
                f"- Recency: {clean(row.get('recency_status'))} ({clean(row.get('recency_evidence'))})",
                f"- Published: {clean(row.get('source_published_at')) or 'Not supplied'}",
                f"- ATS updated: {clean(row.get('source_updated_at')) or 'Not supplied'}",
                f"- Apply type: {clean(row.get('apply_type'))}",
                f"- Link: {clean(row.get('link'))}",
                "",
            ]
        )
    return "\n".join(lines)


def write_outputs(rows: list[dict[str, Any]], *, publish_latest: bool) -> None:
    write_json(ATS_JSON, rows)
    write_csv(ATS_CSV, rows)
    ATS_MD.write_text(markdown(rows), encoding="utf-8")
    if not publish_latest:
        return
    for path in [JOBS_DIR / "latest_jobs.json", JOBS_DIR / "latest_singapore_jobs.json"]:
        write_json(path, rows)
    for path in [JOBS_DIR / "latest_jobs.csv", JOBS_DIR / "latest_singapore_jobs.csv"]:
        write_csv(path, rows)
    for path in [JOBS_DIR / "latest_jobs.md", JOBS_DIR / "latest_singapore_jobs.md"]:
        path.write_text(markdown(rows), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish recent, currently active direct-ATS jobs for the daily digest.")
    parser.add_argument("--include-web", action="store_true")
    parser.add_argument("--publish-latest", action="store_true")
    args = parser.parse_args()

    rows, poll_stamp = current_ats_rows()
    ats_count = len(rows)
    if args.include_web:
        rows = merge_web_rows(rows)
    write_outputs(rows, publish_latest=args.publish_latest)
    status = {
        "status": "SUCCESS" if rows else "NO_RECENT_CURRENT_JOBS",
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ats_poll_started_at": poll_stamp,
        "current_ats_count": ats_count,
        "combined_count": len(rows),
        "web_fallback_included": args.include_web,
    }
    write_json(ATS_STATUS, status)
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
