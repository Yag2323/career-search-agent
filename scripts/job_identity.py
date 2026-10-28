from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from career_store import CareerStore

TRACKING_QUERY_KEYS = {
    "source",
    "src",
    "ref",
    "referrer",
    "trackingid",
    "traceinfo",
    "trk",
    "gh_src",
    "lever-source",
    "campaign",
    "feedid",
}

DIRECT_ATS_DOMAINS = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
    "oraclecloud.com",
    "successfactors.com",
    "taleo.net",
}

AGGREGATOR_DOMAINS = {
    "adzuna.in",
    "adzuna.sg",
    "aijobs.net",
    "builtinsingapore.com",
    "efinancialcareers.cn",
    "efinancialcareers.sg",
    "facebook.com",
    "foundit.sg",
    "grabjobs.co",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "glassdoor.sg",
    "glints.com",
    "internsg.com",
    "jobleads.com",
    "jobstreet.com",
    "jobstreet.com.sg",
    "jobsdb.com",
    "prosple.com",
    "pitchmeai.com",
    "jora.com",
    "jobcube.sg",
    "mycareersfuture.gov.sg",
    "remoterocketship.com",
    "talent.com",
    "wellfound.com",
}


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def canonicalize_url(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        scheme = (parts.scheme or "https").lower()
        host = parts.netloc.lower().removeprefix("www.")
        if (scheme == "https" and host.endswith(":443")) or (scheme == "http" and host.endswith(":80")):
            host = host.rsplit(":", 1)[0]
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=False):
            lower_key = key.lower()
            if lower_key.startswith("utm_") or lower_key in TRACKING_QUERY_KEYS:
                continue
            query.append((key, item))
        return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))
    except Exception:
        return value.split("#", 1)[0]


def domain_of(url: str) -> str:
    try:
        return urlsplit(canonicalize_url(url)).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def normalized_words(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = re.sub(r"\b(?:pte\.? ltd\.?|private limited|limited|ltd\.?|inc\.?|llc|careers?|jobs?)\b", " ", text)
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return " ".join(text.split())


def normalized_job_title(value: str) -> str:
    text = value.lower()
    text = re.sub(
        r"\((?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+20\d{2}\)",
        " ",
        text,
    )
    text = re.sub(r"\s*(?:-|\|)\s*singapore\s*$", " ", text)
    text = re.sub(r"\s+job details\s*(?:\|.*)?$", " ", text)
    text = re.sub(r"\s+-\s+[^-]{2,50}\s+careers\s*$", " ", text)
    return normalized_words(text)


def canonical_key(row: dict[str, Any]) -> str:
    company = normalized_words(clean(row.get("company")))
    title = normalized_words(clean(row.get("title")))
    location = normalized_words(clean(row.get("location")) or "singapore")
    domain = domain_of(clean(row.get("canonical_url") or row.get("link")))
    if not company or company in AGGREGATOR_DOMAINS or company == "unknown":
        company = normalized_words(domain)
    return "|".join([company, title, location])


def stable_job_id(row: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_key(row).encode("utf-8")).hexdigest()[:10].upper()
    return f"JOB-{digest}"


def source_type(url: str) -> str:
    domain = domain_of(url)
    if any(item in domain for item in DIRECT_ATS_DOMAINS):
        return "DIRECT_APPLY"
    if any(item in domain for item in AGGREGATOR_DOMAINS):
        return "AGGREGATOR_OR_MANUAL"
    host_prefix = domain.split(".", 1)[0]
    path = urlsplit(canonicalize_url(url)).path.lower()
    if host_prefix in {"career", "careers", "jobs", "talent"} or re.search(r"/careers?/[^/]+", path):
        return "COMPANY_CAREER_PAGE"
    return "REVIEW"


def is_specific_job_listing(title: str, url: str) -> bool:
    normalized_title = clean(title).lower()
    if re.match(r"^(?:jobs|careers|open positions|job openings)\s+(?:at|with|for)\b", normalized_title):
        return False
    if re.search(r"\b(?:career advice|employment rate|salary in|salary guide)\b", normalized_title):
        return False
    if normalized_title.endswith(" jobs"):
        return False

    canonical = canonicalize_url(url)
    parts = urlsplit(canonical)
    domain = parts.netloc.lower().removeprefix("www.")
    path = parts.path.strip("/")
    segments = [item for item in path.split("/") if item]
    query = parse_qs(parts.query)

    if any(segment in {"career-advice", "salary", "salaries", "news", "blog", "articles"} for segment in segments):
        return False
    if "efinancialcareers." in domain and "q" in query:
        return False

    if any(item in domain for item in {"greenhouse.io"}):
        if "job_board" in path or "for" in query and "jobs" not in segments:
            return False
        return "jobs" in segments or "gh_jid" in query

    if any(item in domain for item in {"lever.co"}):
        return len(segments) >= 2

    if any(item in domain for item in {"ashbyhq.com"}):
        return "ashby_jid" in query or len(segments) >= 2

    if path.lower() in {"", "career", "careers", "job", "jobs", "open-positions", "job-openings"}:
        return False
    if path.lower().endswith(("/career", "/careers", "/jobs", "/open-positions", "/job-openings")):
        return False
    return True


def source_priority(url: str) -> int:
    return {
        "DIRECT_APPLY": 4,
        "COMPANY_CAREER_PAGE": 3,
        "REVIEW": 2,
        "AGGREGATOR_OR_MANUAL": 1,
    }[source_type(url)]


def enrich_known_portal_metadata(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    content = clean(output.get("extracted_content") or output.get("snippet"))
    domain = domain_of(clean(output.get("link")))
    title = clean(output.get("title"))

    if "jobs.lever.co" in domain:
        parts = [clean(item) for item in title.split(" - ") if clean(item)]
        if len(parts) >= 3 and parts[-1].lower() == "singapore":
            output["company"] = parts[0]
            output["title"] = " - ".join(parts[1:-1])

    if "builtinsingapore.com" in domain:
        cleaned_title = re.sub(r"\s*\|\s*Built In Singapore\s*$", "", title, flags=re.IGNORECASE)
        if " - " in cleaned_title:
            role, company = cleaned_title.rsplit(" - ", 1)
            output["title"] = clean(role)
            output["company"] = clean(company)

    if "linkedin.com" in domain and content:
        hiring_match = re.search(
            r"^(.{2,100}?)\s+hiring\s+(.{5,220}?)\s+in\s+Singapore\b",
            content,
            flags=re.IGNORECASE,
        )
        if hiring_match:
            output["company"] = clean(hiring_match.group(1))
            output["title"] = clean(hiring_match.group(2))

    if "bebee.com" in domain:
        page_title = re.match(r"^(.{5,240}?)\s*\|\s*BeBee\b", content, flags=re.IGNORECASE)
        source_title = clean(page_title.group(1)) if page_title else title
        cleaned_title = re.sub(r"\s*\|\s*BeBee\s*$", "", source_title, flags=re.IGNORECASE)
        if " - " in cleaned_title:
            role, company = cleaned_title.rsplit(" - ", 1)
            output["title"] = clean(role)
            output["company"] = clean(company)

    if "glints.com" in domain:
        if " Jobs at " in title:
            output["title"] = clean(title.split(" Jobs at ", 1)[0])
        company_match = re.search(
            r"\bCompany\s+(.{2,100}?)\s+(?:[a-z0-9.-]+\.[a-z]{2,}\s+)?Designation\b",
            content,
            flags=re.IGNORECASE,
        )
        if company_match:
            output["company"] = clean(company_match.group(1))

    if "mycareersfuture.gov.sg" in domain and content:
        company_match = re.search(
            r"\[x\]\s*([A-Z0-9][A-Z0-9 .,&'()/-]{2,100}?)\s+(?:Typically Replies|#)",
            content,
        )
        title_match = re.search(r"#\s*([^#]{8,220}?)\s+MCF-\d{4}-\d+", content, flags=re.IGNORECASE)
        if company_match:
            output["company"] = clean(company_match.group(1))
        if title_match:
            output["title"] = clean(title_match.group(1))
        start = company_match.start() if company_match else max(0, content.lower().find("roles & responsibilities") - 300)
        end_candidates = [
            index
            for marker in [
                "See how you compare with other applicants",
                "Tell employers what skills you have",
                "About the Company",
                "People who viewed this job also viewed",
            ]
            if (index := content.find(marker, start + 1)) >= 0
        ]
        end = min(end_candidates) if end_candidates else len(content)
        job_content = clean(content[start:end])
        if len(job_content) >= 200:
            output["extracted_content"] = job_content[:30000]
            output["snippet"] = job_content[:1800]
    return output


def similarity(left: str, right: str) -> float:
    left_normalized = normalized_words(left)
    right_normalized = normalized_words(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def likely_same_job(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if canonicalize_url(clean(left.get("link"))) == canonicalize_url(clean(right.get("link"))):
        return True
    title_match = SequenceMatcher(
        None,
        normalized_job_title(clean(left.get("title"))),
        normalized_job_title(clean(right.get("title"))),
    ).ratio()
    company_match = similarity(clean(left.get("company")), clean(right.get("company")))
    location_left = normalized_words(clean(left.get("location")) or "singapore")
    location_right = normalized_words(clean(right.get("location")) or "singapore")
    location_match = not location_left or not location_right or location_left == location_right
    generic_companies = {
        "",
        "unknown",
        "singapore",
        "indeed",
        "linkedin",
        "glassdoor",
        "jobstreet",
        "jora",
        "mycareersfuture",
    }
    left_company = normalized_words(clean(left.get("company")))
    right_company = normalized_words(clean(right.get("company")))
    generic_company = (
        left_company in generic_companies
        or right_company in generic_companies
        or any(item in domain_of(clean(left.get("link"))) for item in AGGREGATOR_DOMAINS)
        or any(item in domain_of(clean(right.get("link"))) for item in AGGREGATOR_DOMAINS)
    )
    return title_match >= 0.88 and location_match and (company_match >= 0.78 or (generic_company and title_match >= 0.92))


def merge_text(left: str, right: str, limit: int = 12000) -> str:
    left = clean(left)
    right = clean(right)
    if not left:
        return right[:limit]
    if not right or right.lower() in left.lower():
        return left[:limit]
    if left.lower() in right.lower():
        return right[:limit]
    return f"{left}\n\n{right}"[:limit]


def merge_pair(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    primary_url = clean(primary.get("link"))
    secondary_url = clean(secondary.get("link"))
    if source_priority(secondary_url) > source_priority(primary_url):
        primary, secondary = secondary, primary
        primary_url, secondary_url = secondary_url, primary_url

    output = dict(primary)
    output["snippet"] = merge_text(clean(primary.get("snippet")), clean(secondary.get("snippet")), 5000)
    output["extracted_content"] = merge_text(
        clean(primary.get("extracted_content")), clean(secondary.get("extracted_content")), 30000
    )
    queries = {clean(primary.get("source_query")), clean(secondary.get("source_query"))} - {""}
    output["source_query"] = "; ".join(sorted(queries))
    source_urls = set(filter(None, clean(primary.get("source_urls")).split("; ")))
    source_urls.update(filter(None, clean(secondary.get("source_urls")).split("; ")))
    source_urls.update(filter(None, [canonicalize_url(primary_url), canonicalize_url(secondary_url)]))
    output["source_urls"] = "; ".join(sorted(source_urls))
    output["source_count"] = str(len(source_urls))
    output["canonical_url"] = canonicalize_url(clean(output.get("link")))

    def numeric(item: dict[str, Any], key: str) -> float:
        try:
            return float(item.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    if numeric(secondary, "recency_score") > numeric(primary, "recency_score"):
        for key in [
            "search_time_range",
            "source_published_at_raw",
            "source_published_at",
            "posting_age_hours",
            "recency_status",
            "recency_score",
            "recency_evidence",
            "discovered_at",
        ]:
            output[key] = secondary.get(key, "")
    if numeric(secondary, "degree_fit_score") > numeric(primary, "degree_fit_score"):
        for key in [
            "degree_fit_level",
            "degree_fit_score",
            "degree_families",
            "degree_fit_reason",
            "career_stage",
            "career_stage_score",
        ]:
            output[key] = secondary.get(key, "")
    return output


def find_existing_match(row: dict[str, Any], store: CareerStore) -> str | None:
    canonical_url = canonicalize_url(clean(row.get("link")))
    source_match = store.job_id_for_source(canonical_url)
    if source_match:
        return source_match
    key_match = store.job_id_for_canonical_key(canonical_key(row))
    if key_match:
        return key_match
    for existing in store.list_jobs():
        if likely_same_job(row, existing):
            return clean(existing.get("job_id"))
    return None


def stabilize_and_merge_jobs(
    rows: list[dict[str, Any]],
    store: CareerStore | None = None,
    preserve_legacy_aliases: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    store = store or CareerStore()
    merged: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}

    for raw in rows:
        row = enrich_known_portal_metadata({str(key): clean(value) for key, value in raw.items()})
        old_id = clean(row.get("job_id")).upper()
        row["canonical_url"] = canonicalize_url(clean(row.get("link")))
        row["source_urls"] = row["canonical_url"]
        row["source_count"] = "1"

        in_batch = next((item for item in merged if likely_same_job(row, item)), None)
        if in_batch:
            stable_id = clean(in_batch.get("job_id"))
            updated = merge_pair(in_batch, row)
            updated["job_id"] = stable_id
            merged[merged.index(in_batch)] = updated
        else:
            existing_id = find_existing_match(row, store)
            stable_id = existing_id or stable_job_id(row)
            if existing_id:
                existing = store.get_job(existing_id)
                if existing:
                    row = merge_pair(row, existing)
            row["job_id"] = stable_id
            merged.append(row)

        if preserve_legacy_aliases and old_id and old_id != stable_id:
            aliases[old_id] = stable_id

    for index, row in enumerate(merged):
        row["canonical_url"] = canonicalize_url(clean(row.get("link")))
        row["source_urls"] = clean(row.get("source_urls")) or row["canonical_url"]
        row["source_count"] = str(len(set(filter(None, row["source_urls"].split("; ")))))
        key = canonical_key(row)
        requested_id = clean(row.get("job_id")).upper()
        key_owner = store.job_id_for_canonical_key(key)
        if key_owner and key_owner != requested_id:
            owner = store.get_job(key_owner)
            if owner:
                row = merge_pair(row, owner)
                row["job_id"] = requested_id
                row["canonical_url"] = canonicalize_url(clean(row.get("link")))
                row["source_urls"] = clean(row.get("source_urls")) or row["canonical_url"]
                row["source_count"] = str(len(set(filter(None, row["source_urls"].split("; ")))))
                key = canonical_key(row)

        stable_id = store.upsert_job(row, key)
        if stable_id != requested_id:
            for alias, target in list(aliases.items()):
                if target == requested_id:
                    aliases[alias] = stable_id
        merged[index] = row
        store.ensure_application(clean(row.get("job_id")), clean(row.get("portfolio_project")))
        for source_url in filter(None, row["source_urls"].split("; ")):
            store.register_source(clean(row.get("job_id")), source_url, domain_of(source_url), source_type(source_url))

    for alias, stable_id in aliases.items():
        resolved_id = store.resolve_job_id(stable_id) or stable_id
        store.merge_jobs(alias, resolved_id)
        store.register_alias(alias, resolved_id)

    return merged, aliases
