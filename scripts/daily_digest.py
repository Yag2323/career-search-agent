from __future__ import annotations

import argparse
import csv
from pathlib import Path

from career_agent_common import APPLICATIONS_DIR, JOBS_DIR, clean, send_telegram_file, send_telegram_message
from career_targeting import is_target_degree_role, posting_age_rank, recent_enough_for_digest, recency_rank
from job_delivery import claim_deliveries, filter_undelivered, finalize_deliveries, release_deliveries

DIGEST_PATH = APPLICATIONS_DIR / "daily_digest.md"
SCORED_CSV = JOBS_DIR / "latest_jobs_scored.csv"
LATEST_CSV = JOBS_DIR / "latest_jobs.csv"


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def score_int(row: dict[str, str], key: str = "quality_score") -> int:
    try:
        return int(float(clean(row.get(key))))
    except Exception:
        return 0


def ranked_jobs() -> list[dict[str, str]]:
    jobs = load_csv(SCORED_CSV) or load_csv(LATEST_CSV)
    jobs = [
        row
        for row in jobs
        if clean(row.get("apply_type")).upper() != "SKIP"
        and clean(row.get("availability_status")).upper() == "OPEN"
        and score_int(row, "degree_fit_score") >= 55
        and is_target_degree_role(row)[0]
        and recent_enough_for_digest(row)
    ]
    jobs.sort(
        key=lambda row: (
            recency_rank(row),
            posting_age_rank(row),
            score_int(row, "degree_fit_score"),
            1 if clean(row.get("apply_type")).upper() != "AGGREGATOR_ONLY" else 0,
            score_int(row),
            clean(row.get("job_id")),
        ),
        reverse=True,
    )
    return filter_undelivered(jobs)


def source_authenticity(row: dict[str, str]) -> str:
    mode = clean(row.get("application_mode")).upper()
    apply_type = clean(row.get("apply_type")).upper()
    if mode == "DIRECT_SUPERVISED" or apply_type == "DIRECT_APPLY":
        return "VERIFIED_EMPLOYER_OR_ATS"
    if apply_type == "AGGREGATOR_ONLY":
        return "LIVE_DISCOVERY_LISTING"
    return "LIVE_PAGE_MANUALLY_REVIEWED"


def digest_text(jobs: list[dict[str, str]] | None = None) -> str:
    jobs = ranked_jobs() if jobs is None else jobs

    qualified = list(jobs)
    lines = ["# Daily Career Search Digest", ""]
    lines.extend([f"## New AI & Robotics Degree Opportunities ({len(qualified)} unseen jobs)", ""])
    lines.extend(
        [
            "Every vacancy below is Singapore-based, degree-related, live-checked, and absent from the permanent Telegram delivery history.",
            "This digest contains every vacancy that passed the current search day's qualification checks; no result-count cap is applied.",
            "The search ranks verified posting timestamps first, followed by honest search-window recency when a site does not publish a date.",
            "",
        ]
    )
    for row in qualified:
        lines.extend(
            [
                f"### {clean(row.get('job_id'))} - {clean(row.get('title'))}",
                f"- Company: {clean(row.get('company'))}",
                f"- Quality score: {clean(row.get('quality_score'))}",
                f"- Degree fit: {clean(row.get('degree_fit_level'))} ({clean(row.get('degree_fit_score'))}/100)",
                f"- Degree families: {clean(row.get('degree_families')) or 'Not classified'}",
                f"- Career stage: {clean(row.get('career_stage'))}",
                f"- Posting recency: {clean(row.get('recency_status'))}",
                f"- Published: {clean(row.get('source_published_at')) or 'Timestamp not supplied'}",
                f"- Posting age: {clean(row.get('posting_age_hours')) + ' hours' if clean(row.get('posting_age_hours')) else 'Unknown'}",
                f"- Recency evidence: {clean(row.get('recency_evidence'))}",
                f"- Source authenticity: {source_authenticity(row)}",
                f"- Availability: {clean(row.get('availability_status'))} ({clean(row.get('availability_confidence'))})",
                f"- Checked: {clean(row.get('availability_checked_at'))}",
                f"- Source site: {clean(row.get('application_site'))}",
                f"- Eligibility: {clean(row.get('eligibility_status')) or 'ELIGIBILITY_UNKNOWN'}",
                f"- Requirements: {clean(row.get('matched_count')) or '0'} matched / {clean(row.get('missing_count')) or '0'} missing / {clean(row.get('unknown_count')) or '0'} unknown",
                f"- Sources merged: {clean(row.get('source_count')) or '1'}",
                f"- Link: {clean(row.get('link'))}",
                "",
            ]
        )
    return "\n".join(lines)


def telegram_qualified_jobs_message(jobs: list[dict[str, str]]) -> str:
    qualified = list(jobs)
    lines = [
        f"Daily Career Search: {len(qualified)} new, unseen, confirmed-open opportunities",
        "AI/ML, computer vision, robotics, embedded/IoT, automation, research, data, and aligned graduate engineering roles.",
        "Every vacancy that passed today's checks is included below.",
        "",
    ]
    for index, row in enumerate(qualified, start=1):
        lines.extend(
            [
                f"{index}. {clean(row.get('job_id'))} - {clean(row.get('title'))}",
                f"{clean(row.get('company'))} | {clean(row.get('career_stage'))} | degree fit {clean(row.get('degree_fit_score'))}/100 | quality {clean(row.get('quality_score'))}",
                f"Freshness: {clean(row.get('recency_status'))} ({clean(row.get('recency_evidence'))})",
                f"Source: {source_authenticity(row)} | {clean(row.get('application_site'))}",
                clean(row.get("link")),
                "",
            ]
        )
    if not qualified:
        lines.append("No unseen confirmed-open jobs were found after the expanded search. Prior listings were not resent.")
    return "\n".join(lines).strip()


def telegram_top10_message(jobs: list[dict[str, str]]) -> str:
    """Compatibility alias for callers that still use the former capped name."""
    return telegram_qualified_jobs_message(jobs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and optionally send the daily career-agent digest.")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    jobs = ranked_jobs()
    if args.telegram:
        channel = "DAILY_TELEGRAM_DIGEST"
        qualified = claim_deliveries(jobs, channel=channel)
        text = digest_text(qualified)
        DIGEST_PATH.write_text(text, encoding="utf-8")
        print(f"Digest written: {DIGEST_PATH}")
        try:
            notice_ids = send_telegram_message(telegram_qualified_jobs_message(qualified))
        except Exception:
            release_deliveries(qualified)
            raise
        finalize_deliveries(qualified, channel=channel, message_ids=notice_ids)
        document_id = send_telegram_file(DIGEST_PATH, caption="Daily Career Search Digest")
        print(
            "Telegram digest accepted: "
            f"notice_message_ids={','.join(str(item) for item in notice_ids) or 'unknown'} "
            f"document_message_id={document_id or 'unknown'} "
            f"deliveries_recorded={len(qualified)}"
        )
        return

    text = digest_text(jobs)
    DIGEST_PATH.write_text(text, encoding="utf-8")
    print(f"Digest written: {DIGEST_PATH}")


if __name__ == "__main__":
    main()
