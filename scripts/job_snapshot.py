from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests

from career_agent_common import APPLICATIONS_DIR, JOBS_DIR, clean, find_job, safe_job_id, write_text
from career_store import CareerStore
from job_identity import canonical_key, canonicalize_url, is_specific_job_listing, source_priority, source_type

SNAPSHOT_ROOT = JOBS_DIR / "snapshots"

INACTIVE_PHRASES = [
    "job is no longer available",
    "job is no longer active",
    "job posting is no longer active",
    "position is no longer available",
    "position is no longer open",
    "position has been filled",
    "role has been filled",
    "job has expired",
    "posting has expired",
    "position has expired",
    "vacancy has closed",
    "vacancy is no longer open",
    "applications are closed",
    "applications have closed",
    "application period has ended",
    "no longer accepting applications",
    "this job is closed",
    "this position is closed",
    "this job was removed",
    "job has been removed",
    "closed apply",
    "page not found",
]

ACTIVE_APPLICATION_PHRASES = [
    "apply now",
    "apply for this job",
    "apply for this position",
    "submit application",
    "start application",
    "apply before",
]

TITLE_STOP_TERMS = {
    "career",
    "careers",
    "company",
    "details",
    "engineer",
    "engineering",
    "hiring",
    "intern",
    "internship",
    "job",
    "jobs",
    "position",
    "role",
    "singapore",
}

DEADLINE_PATTERNS = [
    r"(?:apply before|apply by|closing date|application deadline|deadline|closing on)\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
    r"(?:apply before|apply by|closing date|application deadline|deadline|closing on)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})",
    r"(?:apply before|apply by|closing date|application deadline|deadline|closing on)\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    r"(?:apply before|apply by|closing date|application deadline|deadline|closing on)\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
]


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            value = clean(data)
            if value:
                self.parts.append(value)


@dataclass
class VerificationResult:
    job_id: str
    stage: str
    checked_at: str
    source_url: str
    http_status: int | None
    active_status: str
    change_status: str
    content_hash: str
    similarity_to_previous: float | None
    report_path: str
    error: str = ""

    @property
    def safe_for_tailoring(self) -> bool:
        return self.active_status == "ACTIVE" and self.change_status in {"UNCHANGED", "BASELINE_CREATED"}

    @property
    def safe_for_application(self) -> bool:
        return self.active_status == "ACTIVE" and self.change_status in {"UNCHANGED", "BASELINE_CREATED"}


@dataclass
class AvailabilityCheck:
    checked_at: str
    source_url: str
    http_status: int | None
    availability_status: str
    availability_confidence: str
    reason: str
    content_hash: str
    text: str
    application_signal: str = ""
    error: str = ""


def page_text(html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html)
    lines: list[str] = []
    seen: set[str] = set()
    for part in parser.parts:
        normalized = clean(part)
        lower = normalized.lower()
        if len(normalized) < 3:
            continue
        if any(term in lower for term in ["accept all cookies", "cookie preferences", "privacy choices"]):
            continue
        if lower in seen:
            continue
        seen.add(lower)
        lines.append(normalized)
    return "\n".join(lines)


def normalized_content(text: str) -> str:
    output = text.lower()
    output = re.sub(r"https?://\S+", " ", output)
    output = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", output)
    output = re.sub(r"\s+", " ", output)
    return output.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalized_content(text).encode("utf-8")).hexdigest()


def snapshot_path(job_id: str, kind: str, stamp: str) -> Path:
    safe_stamp = re.sub(r"[^0-9A-Za-z]", "", stamp)[:20]
    path = SNAPSHOT_ROOT / job_id / f"{safe_stamp}_{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def capture_search_snapshot(row: dict[str, Any], store: CareerStore | None = None) -> Path | None:
    store = store or CareerStore()
    job_id = clean(row.get("job_id"))
    text = "\n\n".join(
        item for item in [clean(row.get("title")), clean(row.get("snippet")), clean(row.get("extracted_content"))] if item
    )
    if not job_id or not text:
        return None
    digest = content_hash(text)
    previous = store.latest_snapshot(job_id, "search")
    if previous and previous.get("content_hash") == digest:
        return Path(str(previous["content_path"]))
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    path = snapshot_path(job_id, "search", stamp)
    payload = {
        "job_id": job_id,
        "snapshot_kind": "search",
        "captured_at": stamp,
        "source_url": canonicalize_url(clean(row.get("link"))),
        "content_hash": digest,
        "title": clean(row.get("title")),
        "company": clean(row.get("company")),
        "text": text,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    store.add_snapshot(
        job_id,
        "search",
        digest,
        str(path),
        source_url=payload["source_url"],
        active_status="UNKNOWN",
        change_status="BASELINE",
    )
    return path


def capture_search_snapshots(rows: list[dict[str, Any]], store: CareerStore | None = None) -> list[Path]:
    store = store or CareerStore()
    return [path for row in rows if (path := capture_search_snapshot(row, store)) is not None]


def detect_active_status(text: str, status_code: int) -> str:
    lower = normalized_content(text)
    if status_code in {404, 410} or any(phrase in lower for phrase in INACTIVE_PHRASES):
        return "INACTIVE"
    if status_code in {401, 403, 429} or status_code >= 500:
        return "UNVERIFIED"
    return "ACTIVE" if 200 <= status_code < 400 and len(text) >= 120 else "UNVERIFIED"


def explicit_deadline(text: str) -> date | None:
    for pattern in DEADLINE_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw = clean(match.group(1)).replace(",", "")
        for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"]:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def title_is_present(title: str, text: str) -> bool:
    terms = {
        term
        for term in re.findall(r"[a-z0-9+#]{2,}", clean(title).lower())
        if term not in TITLE_STOP_TERMS
    }
    if not terms:
        return False
    required_matches = 1 if len(terms) <= 2 else 2
    visible_lower = text.lower()
    return sum(1 for term in terms if term in visible_lower) >= required_matches


def fetch_job_availability(row: dict[str, Any], url: str, timeout: int = 12) -> AvailabilityCheck:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    source_url = canonicalize_url(url)
    status_code: int | None = None
    text = ""
    error = ""

    if not is_specific_job_listing(clean(row.get("title")), source_url):
        return AvailabilityCheck(
            checked_at=checked_at,
            source_url=source_url,
            http_status=None,
            availability_status="UNVERIFIED",
            availability_confidence="LOW",
            reason="The URL is a job board or careers index, not a specific vacancy page.",
            content_hash="",
            text="",
        )

    try:
        response = requests.get(
            source_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CareerAgentAvailability/1.0",
                "Accept-Language": "en-SG,en;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        status_code = response.status_code
        text = page_text(response.text)
        source_url = canonicalize_url(response.url or source_url)
    except Exception as exc:
        error = f"Live request failed: {type(exc).__name__}: {exc}"

    digest = content_hash(text) if text else ""
    active_status = detect_active_status(text, status_code) if status_code is not None else "UNVERIFIED"
    reason = "The page could not be independently verified."

    deadline = explicit_deadline(text)
    if deadline and deadline < date.today():
        active_status = "INACTIVE"
        reason = f"The listed application deadline ({deadline.isoformat()}) has passed."
    elif active_status == "INACTIVE":
        reason = "The live page returned a closed, expired, filled, or unavailable signal."
    elif active_status == "ACTIVE" and not title_is_present(clean(row.get("title")), text):
        active_status = "UNVERIFIED"
        reason = "The page loaded, but the role title could not be confirmed on the live page."
    elif active_status == "ACTIVE":
        matched_apply_signal = next((phrase for phrase in ACTIVE_APPLICATION_PHRASES if phrase in text.lower()), "")
        direct_source = source_type(source_url) in {"DIRECT_APPLY", "COMPANY_CAREER_PAGE"}
        if not direct_source and not matched_apply_signal and not deadline:
            return AvailabilityCheck(
                checked_at=checked_at,
                source_url=source_url,
                http_status=status_code,
                availability_status="UNVERIFIED",
                availability_confidence="LOW",
                reason="The listing page is live, but no application action or future deadline confirms that applications remain open.",
                content_hash=digest,
                text=text,
                error=error,
            )
        confidence = "HIGH" if direct_source and (matched_apply_signal or deadline) else "MEDIUM"
        application_signal = matched_apply_signal.upper() if matched_apply_signal else (
            f"FUTURE_DEADLINE_{deadline.isoformat()}" if deadline else "DIRECT_PAGE_TITLE_CONFIRMED"
        )
        reason = "Live page, role title, and application availability were confirmed; no closure signal was found."
        return AvailabilityCheck(
            checked_at=checked_at,
            source_url=source_url,
            http_status=status_code,
            availability_status="OPEN",
            availability_confidence=confidence,
            reason=reason,
            content_hash=digest,
            text=text,
            application_signal=application_signal,
            error=error,
        )

    return AvailabilityCheck(
        checked_at=checked_at,
        source_url=source_url,
        http_status=status_code,
        availability_status="CLOSED" if active_status == "INACTIVE" else "UNVERIFIED",
        availability_confidence="HIGH" if active_status == "INACTIVE" and status_code in {404, 410} else "LOW",
        reason=reason,
        content_hash=digest,
        text=text,
        error=error,
    )


def availability_urls(row: dict[str, Any], maximum: int = 3) -> list[str]:
    primary = canonicalize_url(clean(row.get("canonical_url") or row.get("link")))
    candidates = [primary]
    candidates.extend(canonicalize_url(item) for item in clean(row.get("source_urls")).split("; "))
    unique = list(dict.fromkeys(item for item in candidates if item))
    unique.sort(key=lambda item: (source_priority(item), item == primary), reverse=True)
    return unique[:maximum]


def check_row_availability(row: dict[str, Any], timeout: int = 12) -> tuple[dict[str, Any], AvailabilityCheck]:
    checks = [fetch_job_availability(row, url, timeout) for url in availability_urls(row)]
    if not checks:
        checks = [fetch_job_availability(row, clean(row.get("link")), timeout)]

    open_checks = [item for item in checks if item.availability_status == "OPEN"]
    direct_closed = [
        item
        for item in checks
        if item.availability_status == "CLOSED"
        and source_type(item.source_url) in {"DIRECT_APPLY", "COMPANY_CAREER_PAGE"}
    ]
    if open_checks:
        chosen = max(
            open_checks,
            key=lambda item: (source_priority(item.source_url), item.availability_confidence == "HIGH"),
        )
        if direct_closed and source_priority(chosen.source_url) < max(source_priority(item.source_url) for item in direct_closed):
            chosen = max(direct_closed, key=lambda item: source_priority(item.source_url))
    else:
        closed = [item for item in checks if item.availability_status == "CLOSED"]
        chosen = max(closed or checks, key=lambda item: source_priority(item.source_url))

    output = dict(row)
    output.update(
        {
            "availability_status": chosen.availability_status,
            "availability_checked_at": chosen.checked_at,
            "availability_http_status": "" if chosen.http_status is None else str(chosen.http_status),
            "availability_confidence": chosen.availability_confidence,
            "availability_reason": chosen.reason,
            "availability_application_signal": chosen.application_signal,
            "availability_url": chosen.source_url,
            "availability_error": chosen.error,
        }
    )
    if chosen.availability_status == "OPEN":
        output["link"] = chosen.source_url
        output["canonical_url"] = chosen.source_url
        if chosen.text:
            output["extracted_content"] = chosen.text[:30000]
            output["snippet"] = clean(chosen.text[:1800])
    return output, chosen


def store_availability_snapshot(row: dict[str, Any], check: AvailabilityCheck, store: CareerStore) -> None:
    job_id = clean(row.get("job_id"))
    if not job_id:
        return
    path = snapshot_path(job_id, "availability", check.checked_at)
    path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "snapshot_kind": "availability",
                "captured_at": check.checked_at,
                "source_url": check.source_url,
                "http_status": check.http_status,
                "availability_status": check.availability_status,
                "availability_confidence": check.availability_confidence,
                "reason": check.reason,
                "application_signal": check.application_signal,
                "content_hash": check.content_hash,
                "error": check.error,
                "text": check.text[:200000],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    active_status = {
        "OPEN": "ACTIVE",
        "CLOSED": "INACTIVE",
        "UNVERIFIED": "UNVERIFIED",
    }[check.availability_status]
    store.add_snapshot(
        job_id,
        "availability",
        check.content_hash,
        str(path),
        source_url=check.source_url,
        http_status=check.http_status,
        active_status=active_status,
        change_status="CHECKED",
        metadata={"confidence": check.availability_confidence, "reason": check.reason, "error": check.error},
    )
    store.upsert_job(row, canonical_key(row))


def verify_search_rows(
    rows: list[dict[str, Any]],
    timeout: int = 12,
    max_workers: int = 8,
    store: CareerStore | None = None,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    store = store or CareerStore()
    checked: list[tuple[int, dict[str, Any], AvailabilityCheck]] = []
    workers = max(1, min(max_workers, len(rows) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(check_row_availability, row, timeout): index
            for index, row in enumerate(rows)
        }
        completed = 0
        for future in as_completed(futures):
            index = futures[future]
            verified_row, check = future.result()
            checked.append((index, verified_row, check))
            completed += 1
            if progress:
                progress(completed, len(rows), verified_row)

    checked.sort(key=lambda item: item[0])
    output: list[dict[str, Any]] = []
    for _index, row, check in checked:
        store_availability_snapshot(row, check, store)
        output.append(row)
    return output


def write_report(result: VerificationResult, title: str, company: str) -> Path:
    path = APPLICATIONS_DIR / f"{result.job_id}_job_recheck.md"
    lines = [
        f"# Job Recheck - {result.job_id}",
        "",
        f"- Company: {company}",
        f"- Role: {title}",
        f"- Stage: {result.stage}",
        f"- Checked: {result.checked_at}",
        f"- HTTP status: {result.http_status if result.http_status is not None else 'Unavailable'}",
        f"- Active status: {result.active_status}",
        f"- Change status: {result.change_status}",
        f"- Similarity to previous live snapshot: {result.similarity_to_previous if result.similarity_to_previous is not None else 'No previous live snapshot'}",
        f"- Content hash: {result.content_hash or 'Unavailable'}",
        f"- Source: {result.source_url}",
        "",
    ]
    if result.error:
        lines.extend(["## Verification Error", "", result.error, ""])
    if result.active_status == "INACTIVE":
        lines.append("Result: BLOCKED. The vacancy appears inactive.")
    elif result.change_status == "CHANGED":
        lines.append("Result: BLOCKED. The job description changed materially and must be reviewed again.")
    elif result.active_status == "UNVERIFIED":
        lines.append("Result: MANUAL REVIEW. The page could not be independently verified.")
    else:
        lines.append("Result: VERIFIED for this stage.")
    write_text(path, "\n".join(lines) + "\n")
    return path


def recheck_job(
    job_id: str,
    stage: str = "review",
    timeout: int = 25,
    store: CareerStore | None = None,
) -> VerificationResult:
    store = store or CareerStore()
    target = safe_job_id(job_id)
    job = find_job(target)
    url = canonicalize_url(clean(job.get("canonical_url") or job.get("link")))
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    status_code: int | None = None
    text = ""
    error = ""

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CareerAgentVerification/1.0",
                "Accept-Language": "en-SG,en;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        status_code = response.status_code
        text = page_text(response.text)
        url = canonicalize_url(response.url or url)
    except Exception as exc:
        error = f"Live request failed: {type(exc).__name__}: {exc}"

    digest = content_hash(text) if text else ""
    active_status = detect_active_status(text, status_code) if status_code is not None else "UNVERIFIED"
    if active_status == "ACTIVE":
        title_terms = {
            term
            for term in re.findall(r"[a-z0-9+#]{3,}", clean(job.get("title")).lower())
            if term not in {"intern", "engineer", "engineering", "singapore", "careers", "jobs"}
        }
        visible_lower = text.lower()
        required_matches = 1 if len(title_terms) <= 2 else 2
        if title_terms and sum(1 for term in title_terms if term in visible_lower) < required_matches:
            active_status = "UNVERIFIED"
            error = (error + " " if error else "") + "The live page did not contain enough role-title terms to verify this vacancy."
    previous = store.latest_snapshot(target, "live")
    similarity: float | None = None
    if not text:
        change_status = "UNVERIFIED"
    elif not previous:
        change_status = "BASELINE_CREATED"
    elif previous.get("content_hash") == digest:
        change_status = "UNCHANGED"
        similarity = 1.0
    else:
        try:
            previous_payload = json.loads(Path(str(previous["content_path"])).read_text(encoding="utf-8"))
            previous_text = clean(previous_payload.get("text"))
            similarity = round(SequenceMatcher(None, normalized_content(previous_text), normalized_content(text)).ratio(), 3)
        except Exception:
            similarity = 0.0
        change_status = "CHANGED" if similarity < 0.90 else "UNCHANGED"

    path = snapshot_path(target, "live", checked_at)
    path.write_text(
        json.dumps(
            {
                "job_id": target,
                "snapshot_kind": "live",
                "captured_at": checked_at,
                "source_url": url,
                "http_status": status_code,
                "active_status": active_status,
                "change_status": change_status,
                "content_hash": digest,
                "similarity_to_previous": similarity,
                "error": error,
                "text": text[:200000],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store.add_snapshot(
        target,
        "live",
        digest,
        str(path),
        source_url=url,
        http_status=status_code,
        active_status=active_status,
        change_status=change_status,
        metadata={"similarity_to_previous": similarity, "error": error, "stage": stage},
    )
    result = VerificationResult(
        job_id=target,
        stage=stage,
        checked_at=checked_at,
        source_url=url,
        http_status=status_code,
        active_status=active_status,
        change_status=change_status,
        content_hash=digest,
        similarity_to_previous=similarity,
        report_path="",
        error=error,
    )
    report = write_report(result, clean(job.get("title")), clean(job.get("company")))
    result.report_path = str(report)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture and recheck job-description snapshots.")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("job_id")
    recheck = sub.add_parser("recheck")
    recheck.add_argument("job_id")
    recheck.add_argument("--stage", default="review", choices=["review", "tailor", "apply", "fill"])
    recheck.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    target = safe_job_id(args.job_id)
    if args.command == "capture":
        path = capture_search_snapshot(find_job(target))
        print(f"Search snapshot: {path}")
        return

    result = recheck_job(target, stage=args.stage)
    print(json.dumps(asdict(result), indent=2))
    if result.active_status == "INACTIVE" or result.change_status == "CHANGED":
        raise SystemExit(2)
    if args.strict and not result.safe_for_application:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
