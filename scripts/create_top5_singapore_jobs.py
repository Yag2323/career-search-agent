from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import csv
import re

from career_targeting import is_target_degree_role, posting_age_rank, recent_enough_for_digest, recency_rank
from job_delivery import filter_undelivered

BASE = Path(__file__).resolve().parents[1]
JOBS_DIR = BASE / "job_listings"
CSV_PATH = JOBS_DIR / "latest_jobs.csv"
OUT_PATH = JOBS_DIR / "qualified_singapore_jobs.md"
COMPATIBILITY_OUT_PATHS = [
    JOBS_DIR / "top10_singapore_jobs.md",
    JOBS_DIR / "top5_singapore_jobs.md",
]

def clean(value):
    return " ".join(str(value or "").split())

def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

def is_singapore(row):
    location = clean(row.get("location"))
    text = " ".join([
        location,
        clean(row.get("title")),
        clean(row.get("link")),
        clean(row.get("snippet")),
    ]).lower()

    return (
        "singapore" in text
        or ".sg" in text
        or "sg." in domain_of(clean(row.get("link")))
        or bool(re.search(r"(^|[\s,/-])sg($|[\s,/-])", location, flags=re.IGNORECASE))
    )

def score_int(row):
    try:
        return int(float(clean(row.get("quality_score") or row.get("match_score"))))
    except Exception:
        return 0


def field_int(row, key):
    try:
        return int(float(clean(row.get(key))))
    except Exception:
        return 0

def is_confirmed_open(row):
    return clean(row.get("availability_status")).upper() == "OPEN"

def load_rows():
    if not CSV_PATH.exists():
        raise SystemExit(f"Missing file: {CSV_PATH}")

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def main():
    rows = load_rows()

    singapore_rows = [row for row in rows if is_singapore(row) and is_confirmed_open(row)]
    singapore_rows = [row for row in singapore_rows if clean(row.get("apply_type")).upper() != "SKIP"]
    singapore_rows = [row for row in singapore_rows if field_int(row, "degree_fit_score") >= 55]
    singapore_rows = [row for row in singapore_rows if is_target_degree_role(row)[0]]
    singapore_rows = [row for row in singapore_rows if recent_enough_for_digest(row)]
    singapore_rows = filter_undelivered(singapore_rows)

    singapore_rows.sort(
        key=lambda row: (
            recency_rank(row),
            posting_age_rank(row),
            field_int(row, "degree_fit_score"),
            1 if clean(row.get("apply_type")).upper() != "AGGREGATOR_ONLY" else 0,
            score_int(row),
            int(float(clean(row.get("direct_company_score")) or 0)),
            clean(row.get("job_id")),
        ),
        reverse=True,
    )

    primary = [row for row in singapore_rows if clean(row.get("apply_type")).upper() != "AGGREGATOR_ONLY"]
    fallback = [row for row in singapore_rows if clean(row.get("apply_type")).upper() == "AGGREGATOR_ONLY"]
    qualified = primary + fallback

    lines = [
        "# Qualified Singapore AI & Robotics Opportunities",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Source: job_listings/latest_jobs.csv",
        f"Unseen qualified opportunities available: {len(qualified)}.",
        "Rule: previously delivered jobs are excluded permanently; remaining confirmed-open Singapore jobs are ranked by posting recency, AI and Robotics degree fit, early-career fit, and source authenticity.",
        "Quantity: every vacancy passing the current search day's checks is included; no result-count cap is applied.",
        "Freshness: timestamp-confirmed posts are preferred; search-window-only recency is clearly labelled when a provider does not supply the publication time.",
        "",
    ]

    if not qualified:
        lines.extend(["No unseen confirmed-open degree-related opportunities are available in the current search output.", ""])

    for row in qualified:
        lines.extend([
            f"## {clean(row.get('job_id'))}",
            f"- Company: {clean(row.get('company'))}",
            f"- Role: {clean(row.get('title'))}",
            f"- Location: {clean(row.get('location')) or 'Singapore'}",
            f"- Quality score: {clean(row.get('quality_score')) or clean(row.get('match_score'))}",
            f"- Degree fit: {clean(row.get('degree_fit_level'))} ({clean(row.get('degree_fit_score'))}/100)",
            f"- Degree families: {clean(row.get('degree_families')) or 'Not classified'}",
            f"- Career stage: {clean(row.get('career_stage'))}",
            f"- Posting recency: {clean(row.get('recency_status'))}",
            f"- Published: {clean(row.get('source_published_at')) or 'Timestamp not supplied'}",
            f"- Posting age: {clean(row.get('posting_age_hours')) + ' hours' if clean(row.get('posting_age_hours')) else 'Unknown'}",
            f"- Recency evidence: {clean(row.get('recency_evidence'))}",
            f"- Availability: {clean(row.get('availability_status'))} ({clean(row.get('availability_confidence'))})",
            f"- Availability checked: {clean(row.get('availability_checked_at'))}",
            f"- Source site: {clean(row.get('application_site'))}",
            f"- Direct employer/ATS source: {'YES' if clean(row.get('apply_type')).upper() == 'DIRECT_APPLY' else 'NO'}",
            f"- Eligibility: {clean(row.get('eligibility_status')) or 'ELIGIBILITY_UNKNOWN'}",
            (
                f"- Requirement match: {clean(row.get('matched_count')) or '0'} matched, "
                f"{clean(row.get('missing_count')) or '0'} missing, "
                f"{clean(row.get('unknown_count')) or '0'} unknown "
                f"({clean(row.get('match_confidence')) or '0'}% confidence)"
            ),
            f"- Sources merged: {clean(row.get('source_count')) or '1'}",
            f"- Salary/Stipend: {clean(row.get('salary_text')) or 'Not listed'}",
            f"- Deadline: {clean(row.get('application_deadline')) or 'Not listed'}",
            f"- Risk flags: {clean(row.get('risk_flags')) or 'None'}",
            f"- Link: {clean(row.get('link'))}",
            f"- Why: {clean(row.get('quality_reasons')) or 'Scored from Singapore, role, relevance, and fit signals.'}",
            "",
        ])

    text = "\n".join(lines)
    OUT_PATH.write_text(text, encoding="utf-8")
    for compatibility_path in COMPATIBILITY_OUT_PATHS:
        compatibility_path.write_text(text, encoding="utf-8")

    print(f"Created: {OUT_PATH}")
    print("QUALIFIED_JOBS_READY")

if __name__ == "__main__":
    main()
