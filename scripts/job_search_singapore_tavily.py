from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from application_capability import enrich_application_capability
from career_agent_common import merged_env, redact_text
from career_targeting import enrich_targeting, is_target_degree_role, recency_rank
from job_identity import (
    canonicalize_url,
    enrich_known_portal_metadata,
    is_specific_job_listing,
    source_priority,
    source_type,
    stabilize_and_merge_jobs,
)
from job_delivery import filter_undelivered, was_delivered
from career_store import CareerStore
from job_snapshot import capture_search_snapshots, verify_search_rows
from search_provider import SearchConfigurationError, SearchProviderRouter, SearchProviderTerminalError

BASE = Path(__file__).resolve().parents[1]
OUT_DIR = BASE / "job_listings"
LOG_DIR = BASE / "logs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

EARLY_CAREER_TERMS = '(intern OR internship OR apprentice OR apprenticeship OR trainee OR graduate OR "fresh graduate" OR "new grad" OR "entry level" OR junior OR associate)'

OFFICIAL_SOURCE_QUERIES = [
    f'site:boards.greenhouse.io Singapore (AI OR "machine learning" OR robotics OR embedded OR software) {EARLY_CAREER_TERMS}',
    f'site:job-boards.greenhouse.io Singapore (AI OR "machine learning" OR robotics OR embedded OR software) {EARLY_CAREER_TERMS}',
    f'site:jobs.lever.co Singapore (AI OR "machine learning" OR robotics OR embedded OR software) {EARLY_CAREER_TERMS}',
    f'site:jobs.ashbyhq.com Singapore (AI OR robotics OR embedded OR firmware OR software) {EARLY_CAREER_TERMS}',
    f'site:myworkdayjobs.com Singapore (AI OR robotics OR embedded OR automation OR engineering) {EARLY_CAREER_TERMS}',
    f'site:smartrecruiters.com Singapore (AI OR robotics OR embedded OR software OR automation) {EARLY_CAREER_TERMS}',
    f'site:workable.com Singapore (AI OR robotics OR embedded OR software OR automation) {EARLY_CAREER_TERMS}',
    f'Singapore (site:oraclecloud.com OR site:successfactors.com OR site:icims.com) (AI OR robotics OR embedded OR engineering) {EARLY_CAREER_TERMS}',
    f'site:mycareersfuture.gov.sg Singapore (AI OR robotics OR embedded OR automation OR electronics OR software) {EARLY_CAREER_TERMS}',
    f'Singapore (site:careers.gov.sg OR site:careers.a-star.edu.sg) (AI OR robotics OR autonomy OR embedded OR research OR software) {EARLY_CAREER_TERMS}',
]

DEGREE_ROLE_QUERIES = [
    f'Singapore ("AI engineer" OR "artificial intelligence engineer" OR "applied AI engineer") {EARLY_CAREER_TERMS}',
    f'Singapore ("machine learning engineer" OR MLOps OR "model evaluation") {EARLY_CAREER_TERMS}',
    f'Singapore ("generative AI" OR LLM OR NLP OR "AI product engineer") {EARLY_CAREER_TERMS}',
    f'Singapore ("computer vision" OR perception OR "image processing") engineer {EARLY_CAREER_TERMS}',
    f'Singapore ("robotics engineer" OR "robot software" OR "robotic systems") {EARLY_CAREER_TERMS}',
    f'Singapore (SLAM OR localization OR mapping OR navigation OR ROS) (robotics OR autonomous) {EARLY_CAREER_TERMS}',
    f'Singapore ("autonomous systems" OR "autonomous vehicles" OR "motion planning") engineer {EARLY_CAREER_TERMS}',
    f'Singapore ("embedded systems" OR "embedded software" OR "embedded AI") engineer {EARLY_CAREER_TERMS}',
    f'Singapore ("firmware engineer" OR microcontroller OR STM32 OR "edge AI") {EARLY_CAREER_TERMS}',
    f'Singapore ("IoT engineer" OR "electronics engineer" OR "hardware software integration") {EARLY_CAREER_TERMS}',
    f'Singapore ("automation engineer" OR "controls engineer" OR mechatronics) {EARLY_CAREER_TERMS}',
    f'Singapore ("systems engineer" OR "test engineer" OR "validation engineer") (robotics OR embedded OR electronics OR automation) {EARLY_CAREER_TERMS}',
    f'Singapore ("data scientist" OR "data engineer" OR "AI analyst") {EARLY_CAREER_TERMS}',
    f'Singapore ("software engineer" OR "Python developer" OR "backend engineer") (AI OR robotics OR IoT OR graduate) {EARLY_CAREER_TERMS}',
]

OPPORTUNITY_QUERIES = [
    'Singapore ("technology apprenticeship" OR "engineering apprenticeship" OR "AI apprenticeship")',
    'Singapore ("graduate programme" OR "graduate program" OR "engineering development programme") (technology OR AI OR robotics OR software OR electronics)',
    'Singapore ("graduate engineer" OR "engineering graduate" OR "graduate associate") (robotics OR mechatronics OR embedded OR automation OR software)',
    'Singapore ("research engineer" OR "research assistant") (AI OR machine learning OR computer vision OR robotics OR autonomous systems)',
    'Singapore ("project engineer" OR "application engineer") (robotics OR automation OR embedded OR IoT) (graduate OR junior OR entry)',
    'Singapore ("trainee engineer" OR "technical trainee" OR "associate engineer") (electronics OR embedded OR automation OR robotics)',
]

EMPLOYER_CLUSTER_QUERIES = [
    f'Singapore (A*STAR OR DSTA OR HTX OR GovTech OR "ST Engineering") (AI OR robotics OR autonomy OR embedded OR software) {EARLY_CAREER_TERMS}',
    f'Singapore (Dyson OR Micron OR Infineon OR Aumovio OR Bosch OR Thales) (AI OR robotics OR firmware OR embedded OR automation) {EARLY_CAREER_TERMS}',
    f'Singapore (Grab OR Sea OR Shopee OR NCS OR Singtel OR ByteDance) (AI OR machine learning OR data OR software) {EARLY_CAREER_TERMS}',
    f'Singapore (NUS OR NTU OR SUTD OR SIT) ("research engineer" OR "research assistant" OR robotics OR AI) {EARLY_CAREER_TERMS}',
]

SINGAPORE_QUERIES = OFFICIAL_SOURCE_QUERIES + DEGREE_ROLE_QUERIES + OPPORTUNITY_QUERIES + EMPLOYER_CLUSTER_QUERIES
SHORTFALL_QUERIES = SINGAPORE_QUERIES

GOOD_ATS_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
]

MANUAL_ONLY_DOMAINS = [
    "adzuna.sg",
    "aijobs.net",
    "builtinsingapore.com",
    "facebook.com",
    "foundit.sg",
    "grabjobs.co",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "glints.com",
    "internsg.com",
    "jobleads.com",
    "ziprecruiter.com",
    "monster.com",
    "jobstreet.com",
    "jobstreet.com.sg",
    "jobsdb.com",
    "mycareersfuture.gov.sg",
    "prosple.com",
    "pitchmeai.com",
    "remoterocketship.com",
    "talent.com",
    "wellfound.com",
]

def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def numeric(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

def classify_url(url: str) -> str:
    domain = domain_of(url)

    if any(d in domain for d in MANUAL_ONLY_DOMAINS):
        return "MANUAL_REVIEW_ONLY"

    if any(d in domain for d in GOOD_ATS_DOMAINS):
        return "OFFICIAL_OR_ATS"

    return "REVIEW"

def is_singapore_result(title: str, url: str, snippet: str) -> bool:
    text = f"{title} {url} {snippet}".lower()
    domain = domain_of(url)
    title_lower = title.lower()

    foreign_local_domains = [
        "builtinsf.com",
        "builtinnyc.com",
        "builtinaustin.com",
        "builtinboston.com",
        "builtinchicago.org",
        "builtincolorado.com",
        "builtinla.com",
        "builtinseattle.com",
    ]
    if any(item in domain for item in foreign_local_domains):
        return False

    foreign_title_terms = [
        "san francisco",
        "new york",
        "united states",
        "california",
        "london",
        "berlin",
        "australia",
        "india",
        "malaysia",
        "indonesia",
        "philippines",
        "illinois",
        "huntley",
    ]
    if "singapore" not in title_lower and (
        any(term in title_lower for term in foreign_title_terms)
        or re.search(r"\b(?:usa|u\.s\.a\.)\b", title_lower)
    ):
        return False

    explicit_foreign_location = re.search(
        r"\b(?:location|in-office|on-site|based in)\s*[:\-]?\s*"
        r"(?:san francisco|new york|california|illinois|huntley|london|berlin|sydney|melbourne|india|malaysia|indonesia|philippines)\b",
        text,
    )
    if explicit_foreign_location and not re.search(
        r"\b(?:location|in-office|on-site|based in)\s*[:\-]?\s*singapore\b",
        text,
    ):
        return False

    if domain.endswith(".sg"):
        return True

    if ".sg/" in url.lower():
        return True

    singapore_terms = [
        "singapore",
        "singaporean",
        "singaporeans",
        "mycareersfuture",
        "jobstreet.com.sg",
    ]

    if any(term in text for term in singapore_terms):
        return True

    if re.search(r"\bsg\b", text):
        return True

    return False

def is_job_like(title: str, url: str, snippet: str) -> bool:
    text = f"{title} {url} {snippet}".lower()
    title_years = [int(value) for value in re.findall(r"\b20\d{2}\b", title)]
    if title_years and max(title_years) < datetime.now().year:
        return False

    positive_terms = [
        "artificial intelligence",
        "machine learning",
        "computer vision",
        "robotics",
        "embedded",
        "firmware",
        "automation",
        "mechatronics",
        "iot",
        "software",
        "developer",
        "engineer",
        "intern",
        "internship",
        "junior",
        "graduate",
        "fresh graduate",
        "new grad",
        "apprentice",
        "apprenticeship",
        "trainee",
        "entry level",
        "apply",
        "career",
        "job",
        "role",
        "position",
    ]

    negative_terms = [
        "course",
        "bootcamp",
        "career advice",
        "job search",
        "jobs in singapore",
        "jobs - work from home",
        "salary guide",
        "with salaries",
        "internships in singapore",
        "interview questions",
        "resume example",
        "reddit",
        "youtube",
        "news",
        "blog",
        "github.com",
    ]

    if any(term in text for term in negative_terms):
        return False

    if not is_specific_job_listing(title, url):
        return False

    if not any(term in text for term in positive_terms):
        return False
    target, _reason = is_target_degree_role({"title": title, "snippet": snippet, "link": url})
    return target

def guess_company(title: str, url: str) -> str:
    domain = domain_of(url)

    if any(ats in domain for ats in ["lever.co", "greenhouse.io", "ashbyhq.com"]):
        first_title_part = clean(title.split(" - ", 1)[0])
        role_terms = ["engineer", "developer", "intern", "scientist", "analyst", "manager", "specialist"]
        if first_title_part and not any(term in first_title_part.lower() for term in role_terms):
            return first_title_part
        path_parts = [item for item in urlparse(url).path.split("/") if item and item != "embed"]
        if path_parts:
            return re.sub(r"[-_]+", " ", path_parts[0]).title()

    if " - " in title:
        possible = title.split(" - ")[-1].strip()
        if 2 <= len(possible) <= 70:
            return possible

    if "|" in title:
        possible = title.split("|")[-1].strip()
        if 2 <= len(possible) <= 70:
            return possible

    return domain or "Unknown"


def estimate_match_score(title: str, snippet: str, url: str) -> int:
    text = f"{title} {snippet} {url}".lower()
    targeting = enrich_targeting({"title": title, "snippet": snippet, "link": url})
    score = 20 + round(int(targeting.get("degree_fit_score") or 0) * 0.45)

    if "singapore" in text or ".sg" in url.lower():
        score += 15

    if "intern" in text or "internship" in text or "apprentice" in text:
        score += 15

    if "junior" in text or "entry level" in text or "graduate" in text:
        score += 15

    if "software" in text or "developer" in text or "engineer" in text:
        score += 10

    if "remote" in text or "hybrid" in text:
        score += 5

    if classify_url(url) == "OFFICIAL_OR_ATS":
        score += 10

    if classify_url(url) == "MANUAL_REVIEW_ONLY":
        score -= 5

    return max(0, min(score, 100))

def write_csv_atomic(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def write_availability_report(path: Path, title: str, rows: List[Dict[str, Any]]) -> None:
    lines = [
        f"# {title}",
        "",
        f"Generated: {datetime.now().astimezone().isoformat(timespec='minutes')}",
        "",
    ]
    if not rows:
        lines.append("No jobs in this category.")
    for row in rows:
        lines.extend(
            [
                f"## {clean(row.get('job_id'))} - {clean(row.get('title'))}",
                "",
                f"- Company: {clean(row.get('company'))}",
                f"- Availability: {clean(row.get('availability_status'))}",
                f"- Checked: {clean(row.get('availability_checked_at'))}",
                f"- HTTP status: {clean(row.get('availability_http_status')) or 'Unavailable'}",
                f"- Confidence: {clean(row.get('availability_confidence'))}",
                f"- Reason: {clean(row.get('availability_reason'))}",
                f"- Application-open signal: {clean(row.get('availability_application_signal')) or 'Not confirmed'}",
                f"- Site: {clean(row.get('application_site'))}",
                f"- Application mode: {clean(row.get('application_mode'))}",
                f"- Link: {clean(row.get('availability_url') or row.get('link'))}",
                "",
            ]
        )
    write_text_atomic(path, "\n".join(lines))


def availability_progress(done: int, total: int, row: Dict[str, Any]) -> None:
    print(
        f"Live vacancy check {done}/{total}: {clean(row.get('job_id'))} "
        f"{clean(row.get('availability_status'))}"
    )

def main():
    parser = argparse.ArgumentParser(
        description="Singapore-only Career Agent search with five-provider fallback"
    )
    parser.add_argument("--query", help="Single custom Singapore job search query")
    parser.add_argument(
        "--shortfall-mode",
        action="store_true",
        help="Search only apprenticeship, graduate, entry-level, and junior queries after direct ATS feeds fall short.",
    )
    parser.add_argument("--max-results", type=int, default=15, help="Results per query, max 20")
    parser.add_argument("--limit", type=int, default=40, help="Maximum total unique Singapore results")
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=80,
        help="Candidate pages to collect before live verification; must be at least --limit",
    )
    parser.add_argument("--extract-top", type=int, default=30, help="Extract full content from top N results")
    parser.add_argument("--per-query-cap", type=int, default=4, help="Maximum accepted results per query for role diversity")
    parser.add_argument("--time-range", default="day", help="Primary freshness window: day/week/month/year or d/w/m/y")
    parser.add_argument(
        "--fallback-time-range",
        default="week,month",
        help="Comma-separated fallback windows when a query produces no accepted results; use an empty value to disable",
    )
    parser.add_argument(
        "--max-query-attempts",
        type=int,
        default=70,
        help="Maximum provider requests across all queries and freshness tiers",
    )
    args = parser.parse_args()

    env = merged_env()
    try:
        search_router = SearchProviderRouter.from_env(env)
    except SearchConfigurationError as exc:
        raise SystemExit(str(exc)) from exc

    max_results = max(1, min(args.max_results, 20))
    limit = max(1, args.limit)
    candidate_limit = max(limit, args.candidate_limit)
    extract_top = max(0, args.extract_top)
    per_query_cap = max(1, args.per_query_cap)
    time_range = args.time_range.strip() if args.time_range else None
    fallback_time_ranges = [
        item.strip()
        for item in re.split(r"[,;]", args.fallback_time_range or "")
        if item.strip()
    ]
    max_query_attempts = max(1, args.max_query_attempts)
    search_status_path = OUT_DIR / "availability_search_status.json"
    search_run_id = datetime.now().astimezone().strftime("SEARCH-%Y%m%dT%H%M%S%z")
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def write_running_status(stage: str, **progress: Any) -> None:
        write_text_atomic(
            search_status_path,
            json.dumps(
                {
                    "status": "RUNNING",
                    "stage": stage,
                    "started_at": started_at,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "search_run_id": search_run_id,
                    **progress,
                    **search_router.status(),
                },
                indent=2,
            ),
        )

    write_running_status(
        "STARTING",
        query_attempts=0,
        max_query_attempts=max_query_attempts,
        candidates_found=0,
    )

    queries = [args.query] if args.query else (SHORTFALL_QUERIES if args.shortfall_mode else SINGAPORE_QUERIES)

    seen_urls = set()
    seen_ats_sources: set[str] = set()
    ats_discovery_rows: list[dict[str, str]] = []
    rows: List[Dict[str, Any]] = []
    raw_debug: List[Dict[str, Any]] = []
    delivery_store = CareerStore()
    query_attempts = 0
    today = datetime.now().strftime("%Y-%m-%d")

    for query in queries:
        attempts: list[str | None] = []
        for candidate_window in [time_range, *fallback_time_ranges]:
            if candidate_window not in attempts:
                attempts.append(candidate_window)

        for current_time_range in attempts:
            if query_attempts >= max_query_attempts:
                break
            query_attempts += 1
            label = current_time_range or "none"
            before_count = len(rows)

            write_running_status(
                "DISCOVERY",
                query_attempts=query_attempts,
                max_query_attempts=max_query_attempts,
                candidates_found=len(rows),
                current_query=query,
                current_time_range=label,
            )

            print(f"Searching Singapore jobs: {query} | time_range={label}")

            try:
                response = search_router.search(
                    query=query,
                    max_results=max_results,
                    time_range=current_time_range,
                )
            except SearchProviderTerminalError as exc:
                error = redact_text(str(exc), env)
                write_text_atomic(
                    search_status_path,
                    json.dumps(
                        {
                            "status": "FAILED",
                            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "reason": error,
                            **search_router.status(),
                        },
                        indent=2,
                    ),
                )
                raise SystemExit(error) from exc
            except Exception as exc:
                error = redact_text(str(exc), env)
                print(f"[WARN] Search failed: {query} | {error}")
                raw_debug.append({
                    "query": query,
                    "time_range": label,
                    "provider": search_router.active_provider,
                    "error": error,
                })
                continue

            results = response.get("results", [])
            provider = clean(response.get("_provider") or search_router.active_provider).upper()
            print(f"Raw results returned: {len(results)} | provider={provider}")

            raw_debug.append({
                "query": query,
                "time_range": label,
                "provider": provider,
                "credits_used": response.get("_credits_used", ""),
                "raw_result_count": len(results),
                "results": [
                    {
                        "title": clean(item.get("title")),
                        "url": clean(item.get("url")),
                        "content": clean(item.get("content"))[:2000],
                        "score": item.get("score", ""),
                        "date": clean(
                            item.get("published_date")
                            or item.get("publishedAt")
                            or item.get("published_at")
                            or item.get("date")
                        ),
                    }
                    for item in results
                    if isinstance(item, dict)
                ],
            })

            for result in results:
                title = clean(result.get("title"))
                url = clean(result.get("url"))
                snippet = clean(result.get("content"))
                search_provider = clean(result.get("search_provider") or provider).upper()
                provider_score = result.get("score", "")
                raw_content = clean(result.get("raw_content"))
                published_at = clean(
                    result.get("published_date")
                    or result.get("publishedAt")
                    or result.get("published_at")
                    or result.get("date")
                )

                canonical_url = canonicalize_url(url)
                if (
                    url
                    and canonical_url not in seen_ats_sources
                    and source_type(url) == "DIRECT_APPLY"
                ):
                    seen_ats_sources.add(canonical_url)
                    ats_discovery_rows.append(
                        {
                            "company": guess_company(title, url),
                            "title": title,
                            "link": url,
                            "canonical_url": canonical_url,
                            "source_query": query,
                            "search_provider": search_provider,
                        }
                    )
                if not url or canonical_url in seen_urls:
                    continue

                if not is_singapore_result(title, url, snippet):
                    continue

                if not is_job_like(title, url, snippet):
                    continue

                seen_urls.add(canonical_url)

                row = {
                    "job_id": "",
                    "search_run_id": search_run_id,
                    "date_found": today,
                    "discovered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "company": guess_company(title, url),
                    "title": title or "Untitled Singapore job result",
                    "location": "Singapore",
                    "remote_type": "Remote/Hybrid possible" if "remote" in f"{title} {snippet}".lower() or "hybrid" in f"{title} {snippet}".lower() else "Singapore / Unknown",
                    "link": url,
                    "source_query": query,
                    "search_provider": search_provider,
                    "search_time_range": label,
                    "source_published_at_raw": published_at,
                    "source_published_at": published_at,
                    "provider_score": provider_score,
                    "tavily_score": provider_score if search_provider == "TAVILY" else "",
                    "match_score": estimate_match_score(title, snippet, url),
                    "review_type": classify_url(url),
                    "snippet": snippet,
                    "extracted_content": raw_content[:20000],
                    "status": "DISCOVERED",
                }

                enriched = enrich_targeting(row)
                if was_delivered(enriched, delivery_store):
                    continue
                rows.append(enriched)

                if len(rows) >= candidate_limit or len(rows) - before_count >= per_query_cap:
                    break

            if len(rows) > before_count:
                break

            if len(rows) >= candidate_limit:
                break

        if len(rows) >= candidate_limit or query_attempts >= max_query_attempts:
            break

    debug_text = json.dumps(
        {"provider_status": search_router.status(), "searches": raw_debug},
        indent=2,
        ensure_ascii=False,
    )
    for debug_path in [
        LOG_DIR / "search_provider_singapore_raw_debug.json",
        LOG_DIR / "tavily_singapore_raw_debug.json",
    ]:
        debug_path.write_text(debug_text, encoding="utf-8")
    write_text_atomic(
        OUT_DIR / "discovered_ats_sources.json",
        json.dumps(ats_discovery_rows, indent=2, ensure_ascii=False),
    )

    write_running_status(
        "EXTRACTION",
        query_attempts=query_attempts,
        max_query_attempts=max_query_attempts,
        candidates_found=len(rows),
        extraction_targets=min(len(rows), extract_top),
    )

    if not rows:
        write_text_atomic(OUT_DIR / "latest_jobs_checked.json", "[]")
        write_availability_report(OUT_DIR / "closed_jobs_excluded.md", "Closed Jobs Excluded From Current Listings", [])
        write_availability_report(OUT_DIR / "jobs_requiring_verification.md", "Jobs Requiring Manual Availability Verification", [])
        write_text_atomic(
            search_status_path,
            json.dumps(
                {
                    "status": "SUCCESS",
                    "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "search_run_id": search_run_id,
                    "reason": "No unseen Singapore job candidates were returned.",
                    "candidate_count": 0,
                    "open_count": 0,
                    "closed_count": 0,
                    "unverified_count": 0,
                    "query_attempts": query_attempts,
                    **search_router.status(),
                },
                indent=2,
            ),
        )
        print("No unseen Singapore job candidates were returned after the expanded search.")
        return

    urls_to_extract = [row["link"] for row in rows[:extract_top]]
    try:
        extracted = search_router.extract_urls(urls_to_extract)
    except Exception as exc:
        print(f"[WARN] Provider extraction failed: {redact_text(str(exc), env)}")
        extracted = {}

    for row in rows:
        content = extracted.get(row["link"], "")
        if content and not row.get("extracted_content"):
            row["extracted_content"] = content[:20000]

    rows = [enrich_targeting(enrich_known_portal_metadata(row)) for row in rows]
    rows = [
        row
        for row in rows
        if is_singapore_result(
            clean(row.get("title")),
            clean(row.get("link")),
            clean(row.get("extracted_content") or row.get("snippet")),
        )
    ]
    rows = [row for row in rows if is_target_degree_role(row)[0]]
    if not rows:
        write_text_atomic(OUT_DIR / "latest_jobs_checked.json", "[]")
        write_text_atomic(
            search_status_path,
            json.dumps(
                {
                    "status": "SUCCESS",
                    "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "search_run_id": search_run_id,
                    "reason": "No unseen degree-related roles survived precision filtering.",
                    "candidate_count": 0,
                    "open_count": 0,
                    "closed_count": 0,
                    "unverified_count": 0,
                    "query_attempts": query_attempts,
                    **search_router.status(),
                },
                indent=2,
            ),
        )
        print("No unseen degree-related roles survived precision filtering.")
        return

    rows, _aliases = stabilize_and_merge_jobs(rows, preserve_legacy_aliases=False)
    rows = filter_undelivered(rows, delivery_store)
    capture_search_snapshots(rows)
    print(f"Live-checking {len(rows)} stable job pages before publishing current listings.")

    write_running_status(
        "LIVE_VERIFICATION",
        query_attempts=query_attempts,
        max_query_attempts=max_query_attempts,
        candidates_found=len(rows),
        verification_done=0,
        verification_total=len(rows),
    )

    def live_progress(done: int, total: int, row: Dict[str, Any]) -> None:
        availability_progress(done, total, row)
        if done == 1 or done == total or done % 5 == 0:
            write_running_status(
                "LIVE_VERIFICATION",
                query_attempts=query_attempts,
                max_query_attempts=max_query_attempts,
                candidates_found=len(rows),
                verification_done=done,
                verification_total=total,
            )

    checked_rows = verify_search_rows(rows, timeout=12, max_workers=8, progress=live_progress)
    checked_rows = [
        enrich_application_capability(enrich_known_portal_metadata(row))
        for row in checked_rows
    ]

    checked_json_path = OUT_DIR / "latest_jobs_checked.json"
    closed_path = OUT_DIR / "closed_jobs_excluded.md"
    review_path = OUT_DIR / "jobs_requiring_verification.md"
    closed_rows = [row for row in checked_rows if clean(row.get("availability_status")).upper() == "CLOSED"]
    review_rows = [row for row in checked_rows if clean(row.get("availability_status")).upper() == "UNVERIFIED"]
    open_rows = [row for row in checked_rows if clean(row.get("availability_status")).upper() == "OPEN"]

    write_text_atomic(checked_json_path, json.dumps(checked_rows, indent=2, ensure_ascii=False))
    write_availability_report(closed_path, "Closed Jobs Excluded From Current Listings", closed_rows)
    write_availability_report(review_path, "Jobs Requiring Manual Availability Verification", review_rows)

    open_rows.sort(
        key=lambda row: (
            recency_rank(row),
            numeric(row.get("degree_fit_score")),
            source_priority(clean(row.get("availability_url") or row.get("link"))),
            clean(row.get("availability_confidence")).upper() == "HIGH",
            numeric(row.get("match_score")),
            numeric(row.get("provider_score")),
        ),
        reverse=True,
    )
    rows = open_rows[:limit]
    print(
        f"Availability summary: {len(rows)} OPEN retained, {len(closed_rows)} CLOSED excluded, "
        f"{len(review_rows)} UNVERIFIED held for manual review."
    )
    write_text_atomic(
        search_status_path,
        json.dumps(
            {
                "status": "SUCCESS",
                "stage": "COMPLETE",
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "search_run_id": search_run_id,
                "candidate_count": len(checked_rows),
                "open_count": len(rows),
                "closed_count": len(closed_rows),
                "unverified_count": len(review_rows),
                "primary_freshness_window": time_range or "none",
                "fallback_freshness_window": ",".join(fallback_time_ranges) or "disabled",
                "query_attempts": query_attempts,
                **search_router.status(),
            },
            indent=2,
        ),
    )
    csv_path = OUT_DIR / "latest_jobs.csv"
    md_path = OUT_DIR / "latest_jobs.md"
    json_path = OUT_DIR / "latest_jobs.json"

    sg_csv_path = OUT_DIR / "latest_singapore_jobs.csv"
    sg_md_path = OUT_DIR / "latest_singapore_jobs.md"
    sg_json_path = OUT_DIR / "latest_singapore_jobs.json"

    fieldnames = [
        "job_id",
        "date_found",
        "company",
        "title",
        "location",
        "remote_type",
        "link",
        "source_query",
        "search_provider",
        "provider_score",
        "tavily_score",
        "match_score",
        "review_type",
        "snippet",
        "extracted_content",
        "status",
        "canonical_url",
        "source_urls",
        "source_count",
        "availability_status",
        "availability_checked_at",
        "availability_http_status",
        "availability_confidence",
        "availability_reason",
        "availability_application_signal",
        "availability_url",
        "availability_error",
        "application_site",
        "application_mode",
        "agent_site_support",
        "agent_can_prepare",
        "agent_can_submit",
        "application_guidance",
    ]
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    for path in [csv_path, sg_csv_path]:
        write_csv_atomic(path, rows, fieldnames)

    json_text = json.dumps(rows, indent=2, ensure_ascii=False)
    write_text_atomic(json_path, json_text)
    write_text_atomic(sg_json_path, json_text)

    md = [
        "# Latest Singapore Job Search Results",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Location filter: Singapore only. Every included vacancy was independently confirmed OPEN on its live page.",
        "Targeting: AI and Robotics degree roles, including apprenticeships, fresh-graduate programmes, internships, and junior technical work.",
        f"Freshness: searched {time_range or 'no'} window first, then {', '.join(fallback_time_ranges) or 'no fallback'} only when needed. Timestamp evidence is shown per job.",
        f"Search provider path: {' -> '.join(search_router.providers_used) or search_router.active_provider}.",
        "",
        "Important:",
        "- Closed vacancies are excluded; blocked or inconclusive pages are held in jobs_requiring_verification.md.",
        "- Direct employer and ATS sources rank above secondary discovery pages.",
        "- The agent reports links only and does not prepare or submit applications.",
        "",
    ]

    for row in rows:
        md.extend([
            f"## {row['job_id']} — {row['title']}",
            "",
            f"- Company: {row['company']}",
            f"- Location: {row['location']}",
            f"- Remote type: {row['remote_type']}",
            f"- Match score: {row['match_score']}",
            f"- Degree fit: {clean(row.get('degree_fit_level'))} ({clean(row.get('degree_fit_score'))}/100)",
            f"- Degree families: {clean(row.get('degree_families')) or 'Not classified'}",
            f"- Career stage: {clean(row.get('career_stage'))}",
            f"- Posting recency: {clean(row.get('recency_status'))}",
            f"- Published: {clean(row.get('source_published_at')) or 'Timestamp not supplied'}",
            f"- Posting age: {clean(row.get('posting_age_hours')) + ' hours' if clean(row.get('posting_age_hours')) else 'Unknown'}",
            f"- Recency evidence: {clean(row.get('recency_evidence'))}",
            f"- Review type: {row['review_type']}",
            f"- Status: {row['status']}",
            f"- Availability: {row['availability_status']} ({row['availability_confidence']})",
            f"- Availability checked: {row['availability_checked_at']}",
            f"- Availability reason: {row['availability_reason']}",
            f"- Application-open signal: {row['availability_application_signal']}",
            f"- Application site: {row['application_site']}",
            f"- Agent application mode: {row['application_mode']}",
            f"- Agent can prepare: {row['agent_can_prepare']}",
            f"- Agent can submit: {row['agent_can_submit']}",
            f"- Application guidance: {row['application_guidance']}",
            f"- Search provider: {row['search_provider']}",
            f"- Provider score: {row['provider_score'] or 'Not supplied'}",
            f"- Link: {row['link']}",
            f"- Source query: {row['source_query']}",
            "",
            "### Snippet",
            row["snippet"] or "No snippet.",
            "",
        ])

        if row["extracted_content"]:
            md.extend([
                "### Extracted content preview",
                row["extracted_content"][:2500],
                "",
            ])

        md.extend([
            "Recommended next action: REVIEW",
            "",
        ])

    md_text = "\n".join(md)
    write_text_atomic(md_path, md_text)
    write_text_atomic(sg_md_path, md_text)

    print(f"Saved CSV: {csv_path}")
    print(f"Saved Markdown: {md_path}")
    print(f"Saved JSON: {json_path}")
    print(f"Saved Singapore Markdown: {sg_md_path}")
    print(f"Saved checked-candidate audit: {checked_json_path}")
    print(f"Saved closed-job exclusions: {closed_path}")
    print(f"Saved manual-verification queue: {review_path}")
    print(f"Saved search status: {search_status_path}")
    print(f"Saved debug log: {debug_path}")
    print(f"Found {len(rows)} stable, deduplicated, confirmed-open Singapore job records.")

if __name__ == "__main__":
    main()
