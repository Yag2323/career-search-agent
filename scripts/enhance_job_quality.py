from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from career_agent_common import JOBS_DIR, clean
from career_store import CareerStore
from career_targeting import recency_rank
from job_identity import canonical_key, enrich_known_portal_metadata
from job_delivery import filter_undelivered
from job_quality import score_job

CSV_PATH = JOBS_DIR / "latest_jobs.csv"
SCORED_CSV_PATH = JOBS_DIR / "latest_jobs_scored.csv"
SCORED_JSON_PATH = JOBS_DIR / "latest_jobs_scored.json"
SCORED_MD_PATH = JOBS_DIR / "latest_jobs_scored.md"
LATEST_MD_PATH = JOBS_DIR / "latest_jobs.md"
LATEST_SINGAPORE_MD_PATH = JOBS_DIR / "latest_singapore_jobs.md"


def score_int(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(clean(row.get(key))))
    except Exception:
        return default


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"Missing job CSV: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["job_id", "company", "title", "location", "link", "availability_status"]
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_markdown(path: Path, rows: list[dict[str, str]], top: int = 20) -> None:
    ranked = sorted(
        rows,
        key=lambda row: (
            recency_rank(row),
            score_int(row, "degree_fit_score"),
            score_int(row, "quality_score"),
        ),
        reverse=True,
    )
    lines = [
        "# Scored Singapore Job Results",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Ranking uses recent-post evidence, AI and Robotics degree fit, apprenticeship/fresh-graduate fit, Singapore eligibility, direct-company signal, salary/deadline availability, and honest resume evidence.",
        "Only vacancies confirmed OPEN by the search-time live check are scored here.",
        "",
    ]
    for row in ranked[:top]:
        lines.extend(
            [
                f"## {clean(row.get('job_id'))} - {clean(row.get('title'))}",
                "",
                f"- Company: {clean(row.get('company'))}",
                f"- Quality score: {clean(row.get('quality_score'))}",
                f"- Degree fit: {clean(row.get('degree_fit_level'))} ({clean(row.get('degree_fit_score'))}/100)",
                f"- Degree families: {clean(row.get('degree_families')) or 'Not classified'}",
                f"- Career stage: {clean(row.get('career_stage'))}",
                f"- Posting recency: {clean(row.get('recency_status'))}",
                f"- Published: {clean(row.get('source_published_at')) or 'Timestamp not supplied'}",
                f"- Posting age: {clean(row.get('posting_age_hours')) + ' hours' if clean(row.get('posting_age_hours')) else 'Unknown'}",
                f"- Recency evidence: {clean(row.get('recency_evidence'))}",
                f"- Direct employer/ATS source: {'YES' if clean(row.get('apply_type')).upper() == 'DIRECT_APPLY' else 'NO'}",
                f"- Availability: {clean(row.get('availability_status'))} ({clean(row.get('availability_confidence'))})",
                f"- Availability checked: {clean(row.get('availability_checked_at'))}",
                f"- Source site: {clean(row.get('application_site'))}",
                f"- Eligibility: {clean(row.get('eligibility_status'))}",
                (
                    f"- Requirements: {clean(row.get('matched_count')) or '0'} matched, "
                    f"{clean(row.get('missing_count')) or '0'} missing, "
                    f"{clean(row.get('unknown_count')) or '0'} unknown "
                    f"({clean(row.get('match_confidence')) or '0'}% confidence)"
                ),
                f"- Location: {clean(row.get('location'))}",
                f"- Salary/Stipend: {clean(row.get('salary_text')) or 'Not listed'}",
                f"- Deadline: {clean(row.get('application_deadline')) or 'Not listed'}",
                f"- Risk flags: {clean(row.get('risk_flags')) or 'None'}",
                f"- Link: {clean(row.get('link'))}",
                "",
                "Reasons:",
                clean(row.get("quality_reasons")) or "No scored reasons.",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enhance latest jobs with quality scoring and direct-company detection.")
    parser.add_argument("--input", default=str(CSV_PATH), help="Input latest_jobs.csv path")
    parser.add_argument("--no-overwrite", action="store_true", help="Do not overwrite latest_jobs.csv")
    args = parser.parse_args()

    input_path = Path(args.input)
    rows = load_rows(input_path)
    scored = [score_job(enrich_known_portal_metadata(row)) for row in rows]
    scored = filter_undelivered(scored)
    scored.sort(
        key=lambda row: (
            recency_rank(row),
            score_int(row, "degree_fit_score"),
            score_int(row, "quality_score"),
        ),
        reverse=True,
    )
    store = CareerStore()
    for row in scored:
        store.upsert_job(row, canonical_key(row))

    write_csv(SCORED_CSV_PATH, scored)
    SCORED_JSON_PATH.write_text(json.dumps(scored, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown(SCORED_MD_PATH, scored)

    if not args.no_overwrite:
        write_csv(CSV_PATH, scored)
        write_csv(JOBS_DIR / "latest_singapore_jobs.csv", scored)
        write_markdown(LATEST_MD_PATH, scored)
        write_markdown(LATEST_SINGAPORE_MD_PATH, scored)

    print(f"Scored rows: {len(scored)}")
    print(f"CSV: {SCORED_CSV_PATH}")
    print(f"Markdown: {SCORED_MD_PATH}")
    print("JOB_QUALITY_READY")


if __name__ == "__main__":
    main()
