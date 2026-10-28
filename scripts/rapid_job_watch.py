from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import requests

from application_capability import enrich_application_capability
from career_agent_common import JOBS_DIR, STATE_DIR, clean
from career_store import CareerStore
from career_targeting import is_target_degree_role
from job_identity import (
    canonical_key,
    canonicalize_url,
    enrich_known_portal_metadata,
    stable_job_id,
    stabilize_and_merge_jobs,
)
from job_delivery import was_delivered
from job_quality import score_job
from job_snapshot import (
    AvailabilityCheck,
    capture_search_snapshots,
    check_row_availability,
    content_hash,
    store_availability_snapshot,
)

BASE = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE / "config" / "rapid_job_watch.json"
WATCH_STATE_PATH = STATE_DIR / "rapid_job_watch_state.json"
WATCH_STATUS_PATH = STATE_DIR / "rapid_job_watch_status.json"
RAPID_JSON_PATH = JOBS_DIR / "rapid_jobs.json"
RAPID_CSV_PATH = JOBS_DIR / "rapid_jobs.csv"
RAPID_MD_PATH = JOBS_DIR / "rapid_jobs.md"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 120,
    "initial_alert_window_minutes": 1440,
    "max_alerts_per_poll": 10,
    "minimum_quality_score": 45,
    "request_timeout_seconds": 20,
    "workday_max_pages": 5,
    "workday_max_details_per_board": 60,
    "workable_max_details_per_board": 60,
    "auto_discover_boards": True,
    "boards": [],
}

PROVIDER_ORDER = (
    "GREENHOUSE",
    "LEVER",
    "ASHBY",
    "SMARTRECRUITERS",
    "SUCCESSFACTORS_RSS",
    "WORKABLE",
    "WORKDAY_BOARD",
    "WORKDAY",
)
BOARD_COMPANY_ALIASES = {
    "akunacapital": "Akuna Capital",
    "dforeferrals": "Da Vinci",
    "drweng": "DRW",
    "goventi": "Venti Technologies",
    "jumptrading": "Jump Trading",
    "towerresearchcapital": "Tower Research Capital",
    "intrinsicrobotics": "Intrinsic",
    "mistral.ai": "Mistral AI",
    "palo-it": "PALO IT",
    "weride": "WeRide.ai",
    "wehrtyou": "Hudson River Trading",
}
WORKDAY_COMPANY_ALIASES = {
    "ag": "Airbus",
    "automationanywhere": "Automation Anywhere",
    "bostondynamics": "Boston Dynamics",
    "caci": "CACI",
    "cvshealth": "CVS Health",
    "highmarkhealth": "Highmark Health",
    "intel": "Intel",
    "lseg": "LSEG",
    "ntu": "Nanyang Technological University (NTU)",
    "nvidia": "NVIDIA",
    "roche": "Roche",
    "syniverse": "Syniverse",
    "valeo": "Valeo",
}
PLACEHOLDER_COMPANIES = {
    "",
    "unknown",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com",
    "careers",
}

ROLE_PATTERNS = [
    r"\bartificial intelligence\b",
    r"\bai (?:engineer|developer|intern|research|scientist)\b",
    r"\bai\b",
    r"\bmachine learning\b",
    r"\bml (?:engineer|developer|intern|research|scientist)\b",
    r"\bml\b",
    r"\bcomputer vision\b",
    r"\brobot(?:ics|ic)?\b",
    r"\bautonomous (?:systems?|vehicles?|robot)\b",
    r"\bembedded(?: systems?| software)?\b",
    r"\bfirmware\b",
    r"\biot\b",
    r"\binternet of things\b",
    r"\bsoftware (?:engineer|developer|engineering|development)\b",
    r"\bdata (?:scientist|engineer|science)\b",
    r"\b(?:systems?|test|validation|verification|applications?|product|process|equipment|manufacturing) engineer\b",
    r"\b(?:automation|controls?|mechatronics?|electronics?|electrical) engineer\b",
    r"\bresearch (?:engineer|assistant|associate)\b",
    r"\bengineer\b",
]
SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head|architect|vp|vice president)\b",
    flags=re.IGNORECASE,
)
SINGAPORE_PATTERN = re.compile(r"\b(?:singapore|sg)\b", flags=re.IGNORECASE)
SUCCESSFACTORS_SEARCH_TERMS = (
    "artificial intelligence",
    "machine learning",
    "computer vision",
    "robotics",
    "embedded",
    "automation",
    "software engineer",
    "data engineer",
    "graduate",
    "intern",
)


class AtsFeedError(RuntimeError):
    pass


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and clean(data):
            self.parts.append(clean(data))


def html_text(value: Any) -> str:
    parser = VisibleTextParser()
    try:
        parser.feed(str(value or ""))
    except Exception:
        return clean(value)
    return clean(" ".join(parser.parts))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        pass
    return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_watch_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    configured = _read_json(path, {})
    output = dict(DEFAULT_CONFIG)
    if isinstance(configured, dict):
        output.update(configured)
    output["enabled"] = bool(output.get("enabled"))
    output["auto_discover_boards"] = bool(output.get("auto_discover_boards"))
    output["interval_seconds"] = max(60, min(int(output.get("interval_seconds") or 120), 3600))
    output["initial_alert_window_minutes"] = max(
        0, min(int(output.get("initial_alert_window_minutes") or 0), 1440)
    )
    output["max_alerts_per_poll"] = max(1, min(int(output.get("max_alerts_per_poll") or 5), 20))
    output["minimum_quality_score"] = max(0, min(int(output.get("minimum_quality_score") or 45), 100))
    output["request_timeout_seconds"] = max(5, min(int(output.get("request_timeout_seconds") or 20), 60))
    output["workday_max_pages"] = max(1, min(int(output.get("workday_max_pages") or 5), 10))
    output["workday_max_details_per_board"] = max(
        10, min(int(output.get("workday_max_details_per_board") or 60), 120)
    )
    output["workable_max_details_per_board"] = max(
        10, min(int(output.get("workable_max_details_per_board") or 60), 120)
    )
    output["boards"] = output.get("boards") if isinstance(output.get("boards"), list) else []
    return output


def update_watch_config(updates: dict[str, Any], path: Path = CONFIG_PATH) -> dict[str, Any]:
    current = load_watch_config(path)
    current.update(updates)
    _write_json(path, current)
    return load_watch_config(path)


def humanize_board(board: str) -> str:
    if "|" in board:
        host = board.split("|", 1)[0]
        tenant = host.split(".", 1)[0].lower()
        if tenant in WORKDAY_COMPANY_ALIASES:
            return WORKDAY_COMPANY_ALIASES[tenant]
        if 1 < len(tenant) <= 4:
            return tenant.upper()
        return re.sub(r"[-_]+", " ", tenant).title() or board
    if board.lower() in BOARD_COMPANY_ALIASES:
        return BOARD_COMPANY_ALIASES[board.lower()]
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", board)
    value = re.sub(r"[-_.]+", " ", value)
    return clean(value).title() or board


def board_from_url(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(canonicalize_url(url))
    except Exception:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    segments = [item for item in parts.path.split("/") if item]
    query = parse_qs(parts.query)

    if host.endswith("myworkdayjobs.com") or host.endswith("workdayjobs.com"):
        job_index = next(
            (index for index, segment in enumerate(segments) if segment.lower() == "job"),
            -1,
        )
        if job_index < 1 or job_index + 1 >= len(segments):
            return None
        site = segments[job_index - 1]
        job_segments = segments[job_index:]
        action_index = next(
            (
                index
                for index, segment in enumerate(job_segments)
                if segment.lower()
                in {"apply", "applymanually", "autofillwithresume", "usemylastapplication"}
            ),
            len(job_segments),
        )
        external_path = "/" + "/".join(job_segments[:action_index])
        return "WORKDAY", f"{host}|{site}|{external_path}"
    if "greenhouse.io" in host:
        board = clean((query.get("for") or [""])[0])
        if not board and host == "boards-api.greenhouse.io" and len(segments) >= 3:
            board = segments[2]
        if not board and segments and segments[0].lower() not in {"embed", "jobs"}:
            board = segments[0]
        return ("GREENHOUSE", board) if board else None
    if host.endswith("jobs.lever.co") and segments:
        return "LEVER", segments[0]
    if host.endswith("jobs.ashbyhq.com") and segments:
        return "ASHBY", segments[0]
    if host.endswith("jobs.smartrecruiters.com") and segments:
        return "SMARTRECRUITERS", segments[0]
    if host == "apply.workable.com" and segments:
        account = segments[0]
        if account.lower() not in {"j", "jobs", "view"}:
            return "WORKABLE", account
    return None


def workday_board_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    for source in sources:
        if clean(source.get("provider")).upper() != "WORKDAY":
            continue
        try:
            host, site, _external_path = clean(source.get("board")).split("|", 2)
        except ValueError:
            continue
        board = f"{host}|{site}"
        key = ("WORKDAY_BOARD", board.lower())
        output[key] = {
            "provider": "WORKDAY_BOARD",
            "board": board,
            "company": clean(source.get("company")) or humanize_board(board),
        }
    return list(output.values())


def _usable_company(value: str) -> str:
    company = clean(value)
    lowered = company.lower()
    if lowered in PLACEHOLDER_COMPANIES or "myworkdayjobs.com" in lowered or "workdayjobs.com" in lowered:
        return ""
    return company


def discover_boards(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    discovered: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        for url in [clean(row.get("link")), clean(row.get("canonical_url")), clean(row.get("source_urls"))]:
            for source_url in url.split("; "):
                parsed = board_from_url(source_url)
                if not parsed:
                    continue
                provider, board = parsed
                key = (provider, board.lower())
                company = (
                    humanize_board(board)
                    if provider == "WORKDAY"
                    else _usable_company(clean(row.get("company")))
                )
                existing = discovered.get(key)
                if existing is None:
                    discovered[key] = {
                        "provider": provider,
                        "board": board,
                        "company": company or humanize_board(board),
                    }
                elif company and not _usable_company(existing.get("company", "")):
                    existing["company"] = company
    return sorted(
        discovered.values(),
        key=lambda item: (PROVIDER_ORDER.index(item["provider"]), item["board"].lower()),
    )


def merge_boards(configured: list[Any], discovered: list[dict[str, str]]) -> list[dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    for raw in [*configured, *discovered]:
        if not isinstance(raw, dict):
            continue
        provider = clean(raw.get("provider")).upper()
        board = clean(raw.get("board"))
        if provider not in PROVIDER_ORDER or not board:
            continue
        key = (provider, board.lower())
        company = clean(raw.get("company")) or humanize_board(board)
        if key not in output or (
            not _usable_company(output[key].get("company", "")) and _usable_company(company)
        ):
            output[key] = {"provider": provider, "board": board, "company": company}
    return sorted(output.values(), key=lambda item: (PROVIDER_ORDER.index(item["provider"]), item["board"]))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _smart_location(value: Any) -> str:
    if not isinstance(value, dict):
        return clean(value)
    country = value.get("country")
    if isinstance(country, dict):
        country = country.get("name") or country.get("code")
    parts = [value.get("city"), value.get("region"), country]
    return ", ".join(dict.fromkeys(clean(item) for item in parts if clean(item)))


def _nested_label(value: Any) -> str:
    if isinstance(value, dict):
        return clean(value.get("label") or value.get("name") or value.get("id"))
    return clean(value)


def is_potential_target_title(value: Any) -> bool:
    title = clean(value)
    if not title or SENIOR_TITLE_PATTERN.search(title):
        return False
    if re.search(
        r"\b(?:intern(?:ship)?|apprentice(?:ship)?|trainee|graduate|fresh grad(?:uate)?|new grad|entry[ -]level|junior)\b",
        title,
        flags=re.IGNORECASE,
    ):
        return True
    return any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in ROLE_PATTERNS)


def markdown_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    return clean(text)


def workday_posting_age_days(value: Any) -> int:
    text = clean(value).lower()
    if not text:
        return 10_000
    if "today" in text:
        return 0
    if "yesterday" in text:
        return 1
    match = re.search(r"(\d+)\s*(?:day|days)", text)
    if match:
        return int(match.group(1))
    if "30+" in text:
        return 31
    return 10_000


@dataclass
class FeedResult:
    provider: str
    board: str
    entries: list[dict[str, Any]]
    etag: str = ""
    not_modified: bool = False


class AtsFeedClient:
    def __init__(
        self,
        session: Any | None = None,
        timeout: int = 20,
        *,
        workday_max_pages: int = 5,
        workday_max_details_per_board: int = 60,
        workable_max_details_per_board: int = 60,
    ) -> None:
        self.http = session or requests.Session()
        self.timeout = timeout
        self.workday_max_pages = max(1, workday_max_pages)
        self.workday_max_details_per_board = max(1, workday_max_details_per_board)
        self.workable_max_details_per_board = max(1, workable_max_details_per_board)

    @staticmethod
    def _headers(*, etag: str = "", json_content: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-SG,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        }
        if json_content:
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _get(self, url: str, *, params: dict[str, Any], etag: str) -> tuple[Any, str, bool]:
        try:
            response = self.http.get(
                url,
                params=params,
                headers=self._headers(etag=etag),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AtsFeedError("ATS feed could not be reached.") from exc
        response_etag = clean(getattr(response, "headers", {}).get("ETag"))
        if response.status_code == 304:
            return None, response_etag or etag, True
        if response.status_code == 429:
            raise AtsFeedError("ATS feed is temporarily rate limited.")
        if response.status_code in {404, 410}:
            raise AtsFeedError("ATS board was not found.")
        if response.status_code >= 400:
            raise AtsFeedError(f"ATS feed returned HTTP {response.status_code}.")
        try:
            return response.json(), response_etag, False
        except (ValueError, TypeError) as exc:
            raise AtsFeedError("ATS feed returned invalid JSON.") from exc

    def _get_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        try:
            response = self.http.get(
                url,
                params=params or {},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AtsFeedError("ATS feed could not be reached.") from exc
        if response.status_code == 429:
            raise AtsFeedError("ATS feed is temporarily rate limited.")
        if response.status_code in {404, 410}:
            raise AtsFeedError("ATS board was not found.")
        if response.status_code >= 400:
            raise AtsFeedError(f"ATS feed returned HTTP {response.status_code}.")
        return str(response.text or "")

    def _post_json(self, url: str, payload: dict[str, Any]) -> Any:
        try:
            response = self.http.post(
                url,
                json=payload,
                headers=self._headers(json_content=True),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AtsFeedError("ATS feed could not be reached.") from exc
        if response.status_code == 429:
            raise AtsFeedError("ATS feed is temporarily rate limited.")
        if response.status_code in {404, 410}:
            raise AtsFeedError("ATS board was not found.")
        if response.status_code >= 400:
            raise AtsFeedError(f"ATS feed returned HTTP {response.status_code}.")
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise AtsFeedError("ATS feed returned invalid JSON.") from exc

    def fetch_board(self, source: dict[str, str], etag: str = "") -> FeedResult:
        provider = source["provider"]
        board = source["board"]
        company = source.get("company") or humanize_board(board)
        if provider == "WORKDAY_BOARD":
            return self._fetch_workday_board(board, company)
        if provider == "SUCCESSFACTORS_RSS":
            return self._fetch_successfactors_rss(board, company)
        if provider == "WORKABLE":
            return self._fetch_workable_board(board, company)
        if provider == "GREENHOUSE":
            payload, new_etag, unchanged = self._get(
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                params={"content": "true"},
                etag=etag,
            )
            entries = self._greenhouse(payload, board, company) if not unchanged else []
        elif provider == "LEVER":
            payload, new_etag, unchanged = self._get(
                f"https://api.lever.co/v0/postings/{board}",
                params={"mode": "json"},
                etag=etag,
            )
            entries = self._lever(payload, board, company) if not unchanged else []
        elif provider == "ASHBY":
            payload, new_etag, unchanged = self._get(
                f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                params={"includeCompensation": "true"},
                etag=etag,
            )
            entries = self._ashby(payload, board, company) if not unchanged else []
        elif provider == "SMARTRECRUITERS":
            payload, new_etag, unchanged = self._get(
                f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
                params={"limit": 100, "offset": 0},
                etag=etag,
            )
            entries = self._smartrecruiters(payload, board, company) if not unchanged else []
        else:
            try:
                host, site, external_path = board.split("|", 2)
            except ValueError as exc:
                raise AtsFeedError("Workday job source is malformed.") from exc
            tenant = host.split(".", 1)[0]
            payload, new_etag, unchanged = self._get(
                f"https://{host}/wday/cxs/{tenant}/{site}{external_path}",
                params={},
                etag=etag,
            )
            public_url = f"https://{host}/{site}{external_path}"
            logical_board = f"{host}|{site}"
            entries = self._workday(payload, logical_board, company, public_url) if not unchanged else []
        return FeedResult(provider, board, entries, new_etag, unchanged)

    def _fetch_workday_board(self, board: str, company: str) -> FeedResult:
        try:
            host, site = board.split("|", 1)
        except ValueError as exc:
            raise AtsFeedError("Workday board source is malformed.") from exc
        tenant = host.split(".", 1)[0]
        endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        postings: dict[str, dict[str, Any]] = {}

        for page in range(self.workday_max_pages):
            payload = self._post_json(
                endpoint,
                {
                    "appliedFacets": {},
                    "limit": 20,
                    "offset": page * 20,
                    "searchText": "Singapore",
                },
            )
            jobs = payload.get("jobPostings") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise AtsFeedError("Workday board returned an invalid jobs list.")
            for item in jobs:
                if not isinstance(item, dict):
                    continue
                path = clean(item.get("externalPath"))
                title = clean(item.get("title"))
                location = clean(item.get("locationsText"))
                if (
                    path.startswith("/job/")
                    and SINGAPORE_PATTERN.search(location)
                    and is_potential_target_title(title)
                ):
                    postings[path] = item
            total = int(payload.get("total") or 0) if isinstance(payload, dict) else 0
            if len(jobs) < 20 or (total and (page + 1) * 20 >= total):
                break

        ordered = sorted(
            postings.values(),
            key=lambda item: workday_posting_age_days(item.get("postedOn")),
        )[: self.workday_max_details_per_board]
        entries: list[dict[str, Any]] = []
        for item in ordered:
            external_path = clean(item.get("externalPath"))
            detail_url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
            public_url = f"https://{host}/{site}{external_path}"
            try:
                payload, _etag, _unchanged = self._get(detail_url, params={}, etag="")
            except AtsFeedError:
                continue
            mapped = self._workday(payload, board, company, public_url)
            if mapped:
                entries.extend(mapped)
        return FeedResult("WORKDAY_BOARD", board, entries)

    def _fetch_successfactors_rss(self, board: str, company: str) -> FeedResult:
        try:
            host, locale = board.split("|", 1)
        except ValueError as exc:
            raise AtsFeedError("SuccessFactors RSS source is malformed.") from exc
        entries: dict[str, dict[str, Any]] = {}
        successful_queries = 0
        last_error: AtsFeedError | None = None
        for query in SUCCESSFACTORS_SEARCH_TERMS:
            try:
                payload = self._get_text(
                    f"https://{host}/services/rss/job/",
                    params={"locale": locale, "keywords": f"({query})"},
                )
                root = ET.fromstring(payload)
            except AtsFeedError as exc:
                last_error = exc
                continue
            except ET.ParseError:
                last_error = AtsFeedError("SuccessFactors RSS feed returned invalid XML.")
                continue
            successful_queries += 1
            for item in root.findall("./channel/item"):
                raw_title = clean(item.findtext("title"))
                link = canonicalize_url(clean(item.findtext("link")))
                if not raw_title or not link or not is_potential_target_title(raw_title):
                    continue
                location_match = re.search(
                    r"\(([^()]*(?:Singapore|,\s*SG|\bSG))\)\s*$",
                    raw_title,
                    flags=re.IGNORECASE,
                )
                location = clean(location_match.group(1)) if location_match else "Singapore"
                title = clean(raw_title[: location_match.start()]) if location_match else raw_title
                source_id_match = re.search(r"/(\d+)/?$", link)
                source_id = source_id_match.group(1) if source_id_match else hashlib.sha1(link.encode()).hexdigest()[:16]
                published_text = clean(item.findtext("pubDate"))
                try:
                    published = parsedate_to_datetime(published_text).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError):
                    published = published_text
                row = self._common(
                    "SUCCESSFACTORS_RSS",
                    board,
                    source_id,
                    title,
                    company,
                    location,
                    link,
                    html_text(item.findtext("description")),
                    published_at=published,
                )
                entries[row["source_key"]] = row
        if not successful_queries and last_error:
            raise last_error
        return FeedResult("SUCCESSFACTORS_RSS", board, list(entries.values()))

    @staticmethod
    def _workable_index(payload: str) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        for line in str(payload or "").splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [clean(value) for value in line.strip().strip("|").split("|")]
            if len(cells) < 7 or cells[0].lower() == "title" or set(cells[0]) <= {"-", ":"}:
                continue
            detail_match = re.search(
                r"\((https://apply\.workable\.com/[^)]+/jobs/view/([A-Za-z0-9]+)\.md)\)",
                cells[6],
                flags=re.IGNORECASE,
            )
            if not detail_match:
                continue
            output.append(
                {
                    "title": cells[0],
                    "department": cells[1],
                    "location": cells[2],
                    "employment_type": cells[3],
                    "salary_text": "" if cells[4] in {"-", "—"} else cells[4],
                    "published_at": cells[5],
                    "detail_url": detail_match.group(1),
                    "source_id": detail_match.group(2).upper(),
                }
            )
        return output

    @staticmethod
    def _workable_detail(
        payload: str,
        *,
        board: str,
        fallback_company: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        title_match = re.search(r"^#\s+(.+)$", payload, flags=re.MULTILINE)
        meta_match = re.search(r"^>\s*(.+)$", payload, flags=re.MULTILINE)
        meta_parts = [clean(item) for item in meta_match.group(1).split("·")] if meta_match else []
        company = meta_parts[0] if meta_parts else fallback_company
        location = metadata.get("location") or (meta_parts[1] if len(meta_parts) > 1 else "")
        description_match = re.search(
            r"## Description\s*(.*?)(?=\n## Apply|\Z)",
            payload,
            flags=re.IGNORECASE | re.DOTALL,
        )
        description = markdown_text(description_match.group(1) if description_match else payload)
        source_id = clean(metadata.get("source_id")).upper()
        public_url = f"https://apply.workable.com/{board}/j/{source_id}"
        row = AtsFeedClient._common(
            "WORKABLE",
            board,
            source_id,
            title_match.group(1) if title_match else metadata.get("title"),
            company,
            location,
            public_url,
            description,
            published_at=metadata.get("published_at"),
            employment_type=metadata.get("employment_type"),
        )
        row["salary_text"] = clean(metadata.get("salary_text"))
        row["department"] = clean(metadata.get("department"))
        return row

    def _fetch_workable_board(self, board: str, company: str) -> FeedResult:
        index = self._get_text(
            f"https://apply.workable.com/{board}/jobs.md",
            params={"location[0][country]": "Singapore"},
        )
        rows = [
            row
            for row in self._workable_index(index)
            if SINGAPORE_PATTERN.search(clean(row.get("location")))
            and is_potential_target_title(row.get("title"))
        ]
        rows.sort(
            key=lambda row: parse_timestamp(row.get("published_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        entries: list[dict[str, Any]] = []
        for metadata in rows[: self.workable_max_details_per_board]:
            try:
                detail = self._get_text(metadata["detail_url"])
            except AtsFeedError:
                continue
            entries.append(
                self._workable_detail(
                    detail,
                    board=board,
                    fallback_company=company,
                    metadata=metadata,
                )
            )
        return FeedResult("WORKABLE", board, entries)

    @staticmethod
    def _common(
        provider: str,
        board: str,
        source_id: Any,
        title: Any,
        company: Any,
        location: Any,
        link: Any,
        description: Any,
        published_at: Any = "",
        updated_at: Any = "",
        employment_type: Any = "",
        remote_type: Any = "",
        experience_level: Any = "",
    ) -> dict[str, Any]:
        source_id_text = clean(source_id)
        return {
            "source_key": f"{provider}:{board.lower()}:{source_id_text}",
            "ats_provider": provider,
            "ats_board": board,
            "ats_source_id": source_id_text,
            "title": clean(title),
            "company": clean(company) or humanize_board(board),
            "location": clean(location),
            "link": clean(link),
            "snippet": clean(description)[:5000],
            "extracted_content": clean(description)[:30000],
            "source_published_at": iso_utc(parsed) if (parsed := parse_timestamp(published_at)) else "",
            "source_updated_at": iso_utc(parsed) if (parsed := parse_timestamp(updated_at)) else "",
            "employment_type": clean(employment_type),
            "remote_type": clean(remote_type),
            "source_experience_level": clean(experience_level),
        }

    def _greenhouse(self, payload: Any, board: str, company: str) -> list[dict[str, Any]]:
        jobs = payload.get("jobs") if isinstance(payload, dict) else []
        return [
            self._common(
                "GREENHOUSE",
                board,
                item.get("id"),
                item.get("title"),
                company,
                (item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else "",
                item.get("absolute_url"),
                html_text(item.get("content")),
                updated_at=item.get("updated_at"),
            )
            for item in jobs or []
            if isinstance(item, dict) and item.get("id") and item.get("absolute_url")
        ]

    def _lever(self, payload: Any, board: str, company: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
            lists = item.get("lists") if isinstance(item.get("lists"), list) else []
            list_text = " ".join(
                html_text(entry.get("content")) for entry in lists if isinstance(entry, dict)
            )
            description = clean(
                " ".join(
                    [
                        clean(item.get("descriptionPlain")),
                        html_text(item.get("description")),
                        html_text(item.get("additionalPlain")),
                        list_text,
                    ]
                )
            )
            output.append(
                self._common(
                    "LEVER",
                    board,
                    item.get("id"),
                    item.get("text"),
                    company,
                    categories.get("location"),
                    item.get("hostedUrl") or item.get("applyUrl"),
                    description,
                    published_at=item.get("createdAt"),
                    employment_type=categories.get("commitment"),
                    remote_type=categories.get("workplaceType"),
                )
            )
        return output

    def _ashby(self, payload: Any, board: str, company: str) -> list[dict[str, Any]]:
        jobs = payload.get("jobs") if isinstance(payload, dict) else []
        output: list[dict[str, Any]] = []
        for item in jobs or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            locations = [clean(item.get("location"))]
            for secondary in item.get("secondaryLocations") or []:
                if isinstance(secondary, dict):
                    locations.append(clean(secondary.get("location")))
                else:
                    locations.append(clean(secondary))
            output.append(
                self._common(
                    "ASHBY",
                    board,
                    item.get("id"),
                    item.get("title"),
                    company,
                    "; ".join(dict.fromkeys(filter(None, locations))),
                    item.get("jobUrl") or item.get("applyUrl"),
                    item.get("descriptionPlain") or html_text(item.get("descriptionHtml")),
                    published_at=item.get("publishedAt"),
                    employment_type=item.get("employmentType"),
                    remote_type="Remote" if item.get("isRemote") else "On-site/Hybrid",
                )
            )
        return output

    def _smartrecruiters(self, payload: Any, board: str, company: str) -> list[dict[str, Any]]:
        jobs = payload.get("content") if isinstance(payload, dict) else []
        output: list[dict[str, Any]] = []
        for item in jobs or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            title = clean(item.get("name"))
            company_data = item.get("company") if isinstance(item.get("company"), dict) else {}
            link = clean(item.get("postingUrl") or item.get("applyUrl"))
            if not link:
                link = f"https://jobs.smartrecruiters.com/{board}/{item.get('id')}-{_slug(title)}"
            description = " ".join(
                filter(
                    None,
                    [
                        _nested_label(item.get("function")),
                        _nested_label(item.get("experienceLevel")),
                        _nested_label(item.get("typeOfEmployment")),
                    ],
                )
            )
            output.append(
                self._common(
                    "SMARTRECRUITERS",
                    board,
                    item.get("id"),
                    title,
                    company_data.get("name") or company,
                    _smart_location(item.get("location")),
                    link,
                    description,
                    published_at=item.get("releasedDate") or item.get("createdOn"),
                    updated_at=item.get("updatedOn"),
                    employment_type=_nested_label(item.get("typeOfEmployment")),
                    experience_level=_nested_label(item.get("experienceLevel")),
                )
            )
        return output

    def _workday(
        self,
        payload: Any,
        board: str,
        company: str,
        public_url: str,
    ) -> list[dict[str, Any]]:
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or info.get("canApply") is False or info.get("posted") is False:
            return []
        requisition_location = (
            info.get("jobRequisitionLocation")
            if isinstance(info.get("jobRequisitionLocation"), dict)
            else {}
        )
        country = info.get("country") if isinstance(info.get("country"), dict) else {}
        location = clean(
            info.get("location")
            or requisition_location.get("descriptor")
            or country.get("descriptor")
        )
        link = clean(info.get("externalUrl")) or public_url
        source_id = info.get("jobReqId") or info.get("jobPostingId") or info.get("id")
        if not source_id or not info.get("title") or not link:
            return []
        return [
            self._common(
                "WORKDAY",
                board,
                source_id,
                info.get("title"),
                company,
                location,
                link,
                html_text(info.get("jobDescription")),
                published_at=info.get("startDate"),
                employment_type=info.get("timeType"),
            )
        ]


def is_target_job(entry: dict[str, Any]) -> tuple[bool, str]:
    location = clean(entry.get("location"))
    if not SINGAPORE_PATTERN.search(location):
        return False, "Location is not confirmed as Singapore."
    title = clean(entry.get("title"))
    if SENIOR_TITLE_PATTERN.search(title):
        return False, "Senior-level title is outside the target profile."
    target, reason = is_target_degree_role(entry)
    if not target:
        return False, reason
    return True, "Singapore role matches the AI and Robotics degree families."


def _score_int(row: dict[str, Any], key: str = "quality_score") -> int:
    try:
        return int(float(clean(row.get(key))))
    except (TypeError, ValueError):
        return 0


def _freshness_label(row: dict[str, Any], detected_at: datetime, interval_seconds: int) -> str:
    published = parse_timestamp(row.get("source_published_at"))
    if published:
        seconds = max(0, int((detected_at - published).total_seconds()))
        if seconds < 60:
            return f"POSTED {seconds} SECONDS AGO"
        if seconds < 3600:
            return f"POSTED {seconds // 60} MINUTES AGO"
        if seconds < 86400:
            return f"POSTED {seconds // 3600} HOURS AGO"
        return f"POSTED {seconds // 86400} DAYS AGO"
    minutes = max(1, (interval_seconds + 59) // 60)
    return f"NEWLY DETECTED WITHIN {minutes} MINUTES; POSTING TIME NOT SUPPLIED"


def _is_recent_published(entry: dict[str, Any], now: datetime, minutes: int) -> bool:
    published = parse_timestamp(entry.get("source_published_at"))
    if not published or minutes <= 0:
        return False
    age = (now - published).total_seconds()
    return -300 <= age <= minutes * 60


def _is_recent_for_daily_scan(entry: dict[str, Any], now: datetime, days: int = 30) -> bool:
    timestamps = [
        parsed
        for field in ["source_published_at", "source_updated_at"]
        if (parsed := parse_timestamp(entry.get(field))) is not None
    ]
    if not timestamps:
        return False
    age = (now - max(timestamps)).total_seconds()
    return -300 <= age <= max(1, days) * 86400


def _load_discovery_rows(store: CareerStore) -> list[dict[str, Any]]:
    rows = store.list_jobs()
    for path in [
        JOBS_DIR / "latest_jobs.json",
        JOBS_DIR / "latest_jobs_checked.json",
        JOBS_DIR / "discovered_ats_sources.json",
    ]:
        payload = _read_json(path, [])
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
    return rows


def _initial_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "initialized": False,
        "initialized_at": "",
        "sources": {},
        "boards": {},
    }


def _write_rapid_outputs(rows: list[dict[str, Any]], json_path: Path, csv_path: Path, md_path: Path) -> None:
    rows = sorted(
        rows,
        key=lambda row: (clean(row.get("detected_at")), _score_int(row)),
        reverse=True,
    )[:200]
    _write_json(json_path, rows)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    temporary = csv_path.with_name(csv_path.name + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(csv_path)

    lines = [
        "# Rapid Singapore Job Alerts",
        "",
        "These vacancies were newly detected through public employer ATS feeds and checked as open.",
        "A posting-age label is used only when the ATS supplies a publish timestamp.",
        "",
    ]
    if not rows:
        lines.append("No rapid alerts have been produced yet.")
    for row in rows:
        lines.extend(
            [
                f"## {clean(row.get('job_id'))} - {clean(row.get('title'))}",
                "",
                f"- Company: {clean(row.get('company'))}",
                f"- Freshness: {clean(row.get('freshness_label'))}",
                f"- Published by ATS: {clean(row.get('source_published_at')) or 'Not supplied'}",
                f"- Detected: {clean(row.get('detected_at'))}",
                f"- Quality score: {clean(row.get('quality_score'))}",
                f"- Apply type: {clean(row.get('apply_type'))}",
                f"- Application mode: {clean(row.get('application_mode'))}",
                f"- Availability: {clean(row.get('availability_status'))} ({clean(row.get('availability_confidence'))})",
                f"- ATS: {clean(row.get('ats_provider'))} / {clean(row.get('ats_board'))}",
                f"- Link: {clean(row.get('link'))}",
                "",
            ]
        )
    temporary_md = md_path.with_name(md_path.name + ".tmp")
    temporary_md.write_text("\n".join(lines), encoding="utf-8")
    temporary_md.replace(md_path)


class RapidJobWatcher:
    def __init__(
        self,
        *,
        config_path: Path = CONFIG_PATH,
        state_path: Path = WATCH_STATE_PATH,
        status_path: Path = WATCH_STATUS_PATH,
        json_path: Path = RAPID_JSON_PATH,
        csv_path: Path = RAPID_CSV_PATH,
        md_path: Path = RAPID_MD_PATH,
        store: CareerStore | None = None,
        feed_client: Any | None = None,
        row_loader: Callable[[], list[dict[str, Any]]] | None = None,
        availability_checker: Callable[[dict[str, Any], int], tuple[dict[str, Any], AvailabilityCheck]] = check_row_availability,
        scorer: Callable[[dict[str, Any]], dict[str, Any]] = score_job,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.config_path = config_path
        self.state_path = state_path
        self.status_path = status_path
        self.json_path = json_path
        self.csv_path = csv_path
        self.md_path = md_path
        self.store = store or CareerStore()
        self.feed_client = feed_client
        self.row_loader = row_loader or (lambda: _load_discovery_rows(self.store))
        self.availability_checker = availability_checker
        self.scorer = scorer
        self.clock = clock

    def _candidate_row(self, entry: dict[str, Any], detected_at: datetime, config: dict[str, Any]) -> dict[str, Any]:
        row = {
            **entry,
            "job_id": "",
            "date_found": detected_at.date().isoformat(),
            "first_seen": iso_utc(detected_at),
            "detected_at": iso_utc(detected_at),
            "location": clean(entry.get("location")) or "Singapore",
            "remote_type": clean(entry.get("remote_type")) or "Singapore / Unknown",
            "source_query": "RAPID_DIRECT_ATS_WATCH",
            "search_provider": f"DIRECT_ATS_{clean(entry.get('ats_provider')).upper()}",
            "provider_score": "1.0",
            "review_type": "OFFICIAL_OR_ATS",
            "status": "DISCOVERED",
            "canonical_url": canonicalize_url(clean(entry.get("link"))),
            "source_urls": canonicalize_url(clean(entry.get("link"))),
            "source_count": "1",
            "freshness_label": _freshness_label(entry, detected_at, config["interval_seconds"]),
        }
        existing_id = self.store.job_id_for_source(row["canonical_url"])
        existing_id = existing_id or self.store.job_id_for_canonical_key(canonical_key(row))
        row["job_id"] = existing_id or stable_job_id(row)
        return row

    @staticmethod
    def _feed_open_check(row: dict[str, Any], original: AvailabilityCheck) -> AvailabilityCheck:
        text = clean(row.get("extracted_content") or row.get("snippet") or row.get("title"))
        return AvailabilityCheck(
            checked_at=original.checked_at,
            source_url=canonicalize_url(clean(row.get("link"))),
            http_status=original.http_status,
            availability_status="OPEN",
            availability_confidence="HIGH",
            reason="Vacancy is present in the employer's public ATS feed during this poll; the page check was inconclusive.",
            content_hash=content_hash(text),
            text=text,
            application_signal="Public employer ATS feed lists the vacancy as active.",
            error=original.error,
        )

    def poll(
        self,
        *,
        force: bool = False,
        scan_current: bool = False,
        scan_window_days: int = 120,
    ) -> dict[str, Any]:
        config = load_watch_config(self.config_path)
        started = self.clock().astimezone(timezone.utc)
        if not config["enabled"] and not force:
            status = {
                "status": "DISABLED",
                "enabled": False,
                "interval_seconds": config["interval_seconds"],
                "checked_at": iso_utc(started),
                "new_jobs": [],
            }
            _write_json(self.status_path, status)
            return status

        state = _read_json(self.state_path, _initial_state())
        if not isinstance(state, dict):
            state = _initial_state()
        state.setdefault("sources", {})
        state.setdefault("boards", {})
        was_initialized = bool(state.get("initialized"))
        known_board_keys = set(state["boards"])

        stale_cutoff = started - timedelta(minutes=config["initial_alert_window_minutes"])
        for source_state in state["sources"].values():
            if clean(source_state.get("decision")).upper() not in {"PENDING", "PENDING_READY"}:
                continue
            published = parse_timestamp(source_state.get("published_at"))
            if published and published < stale_cutoff:
                source_state["decision"] = "BASELINED_STALE"
                source_state["decision_reason"] = "Outside the rapid-alert freshness window."

        rows = self.row_loader() if config["auto_discover_boards"] else []
        boards = merge_boards(config["boards"], discover_boards(rows))
        if scan_current:
            full_workday_boards = workday_board_sources(boards)
            full_workday_keys = {clean(source.get("board")).lower() for source in full_workday_boards}
            boards = [
                source
                for source in boards
                if source["provider"] != "WORKDAY"
                or "|".join(clean(source.get("board")).split("|")[:2]).lower()
                not in full_workday_keys
            ]
            boards = merge_boards(boards, full_workday_boards)
        else:
            boards = [
                source
                for source in boards
                if source["provider"] not in {"SUCCESSFACTORS_RSS", "WORKDAY", "WORKDAY_BOARD"}
            ]
        client = self.feed_client or AtsFeedClient(
            timeout=config["request_timeout_seconds"],
            workday_max_pages=config["workday_max_pages"],
            workday_max_details_per_board=config["workday_max_details_per_board"],
            workable_max_details_per_board=config["workable_max_details_per_board"],
        )
        errors: list[dict[str, str]] = []
        entries: list[dict[str, Any]] = []
        successful_boards = 0
        unchanged_boards = 0

        workers = max(1, min(12, len(boards) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for source in boards:
                board_key = f"{source['provider']}:{source['board'].lower()}"
                board_state = state["boards"].get(board_key, {})
                etag = "" if scan_current else clean(board_state.get("etag"))
                future = executor.submit(client.fetch_board, source, etag)
                futures[future] = (source, board_key, board_state)

            for future in as_completed(futures):
                source, board_key, board_state = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, AtsFeedError) else "Unexpected ATS feed failure."
                    errors.append({"provider": source["provider"], "board": source["board"], "reason": reason})
                    state["boards"][board_key] = {
                        **board_state,
                        "last_status": "ERROR",
                        "last_checked_at": iso_utc(started),
                        "error": reason,
                    }
                    continue

                successful_boards += 1
                if result.not_modified:
                    unchanged_boards += 1
                entries.extend(result.entries)
                state["boards"][board_key] = {
                    "etag": result.etag,
                    "last_status": "NOT_MODIFIED" if result.not_modified else "OK",
                    "last_checked_at": iso_utc(started),
                    "entry_count": len(result.entries),
                }

        new_entries: list[dict[str, Any]] = []
        baseline_count = 0
        stamp = iso_utc(started)
        for entry in entries:
            source_key = clean(entry.get("source_key"))
            if not source_key:
                continue
            previous = state["sources"].get(source_key)
            state["sources"][source_key] = {
                **(previous or {}),
                "first_seen": clean((previous or {}).get("first_seen")) or stamp,
                "last_seen": stamp,
                "provider": clean(entry.get("ats_provider")),
                "board": clean(entry.get("ats_board")),
                "published_at": clean(entry.get("source_published_at")),
            }
            if previous is None:
                entry_board_key = f"{clean(entry.get('ats_provider')).upper()}:{clean(entry.get('ats_board')).lower()}"
                should_process = (
                    (scan_current and _is_recent_for_daily_scan(entry, started, scan_window_days))
                    or (was_initialized and entry_board_key in known_board_keys)
                    or _is_recent_published(entry, started, config["initial_alert_window_minutes"])
                )
                state["sources"][source_key]["decision"] = "PENDING" if should_process else "BASELINED"
                if should_process:
                    new_entries.append(entry)
                else:
                    baseline_count += 1
            elif scan_current and _is_recent_for_daily_scan(entry, started, scan_window_days):
                state["sources"][source_key]["decision"] = "PENDING"
                new_entries.append(entry)
            elif clean(previous.get("decision")).upper() in {"PENDING", "PENDING_READY"}:
                new_entries.append(entry)

        alerts: list[dict[str, Any]] = []
        relevant_count = 0
        open_count = 0
        for entry in new_entries:
            source_key = clean(entry.get("source_key"))
            target, reason = is_target_job(entry)
            if not target:
                state["sources"][source_key]["decision"] = "FILTERED_ROLE"
                state["sources"][source_key]["decision_reason"] = reason
                continue
            relevant_count += 1
            row = self._candidate_row(entry, started, config)
            try:
                verified, check = self.availability_checker(row, config["request_timeout_seconds"])
            except Exception:
                state["sources"][source_key]["decision"] = "PENDING"
                state["sources"][source_key]["decision_reason"] = "Live page check failed and will be retried."
                continue
            if check.availability_status == "CLOSED":
                state["sources"][source_key]["decision"] = "CLOSED"
                state["sources"][source_key]["decision_reason"] = check.reason
                continue
            if check.availability_status != "OPEN":
                check = self._feed_open_check(verified, check)
                verified.update(
                    {
                        "availability_status": check.availability_status,
                        "availability_checked_at": check.checked_at,
                        "availability_http_status": "" if check.http_status is None else str(check.http_status),
                        "availability_confidence": check.availability_confidence,
                        "availability_reason": check.reason,
                        "availability_application_signal": check.application_signal,
                        "availability_url": check.source_url,
                        "availability_error": check.error,
                    }
                )
            open_count += 1
            verified = enrich_application_capability(enrich_known_portal_metadata(verified))
            scored = self.scorer(verified)
            if clean(scored.get("apply_type")).upper() == "SKIP":
                state["sources"][source_key]["decision"] = "FILTERED_QUALITY"
                state["sources"][source_key]["decision_reason"] = "Quality and eligibility scoring recommends SKIP."
                continue
            if _score_int(scored) < config["minimum_quality_score"]:
                state["sources"][source_key]["decision"] = "FILTERED_QUALITY"
                state["sources"][source_key]["decision_reason"] = (
                    f"Quality score is below {config['minimum_quality_score']}."
                )
                continue

            stable_rows, _aliases = stabilize_and_merge_jobs(
                [scored],
                store=self.store,
                preserve_legacy_aliases=False,
            )
            stable = stable_rows[0]
            if was_delivered(stable, self.store):
                state["sources"][source_key]["job_id"] = clean(stable.get("job_id"))
                state["sources"][source_key]["decision"] = "ALREADY_DELIVERED"
                state["sources"][source_key]["decision_reason"] = "This vacancy was already sent to Telegram."
                continue
            capture_search_snapshots([stable], store=self.store)
            store_availability_snapshot(stable, check, self.store)
            state["sources"][source_key]["job_id"] = clean(stable.get("job_id"))
            state["sources"][source_key]["decision"] = "READY"
            alerts.append(stable)

        alerts.sort(
            key=lambda row: (parse_timestamp(row.get("source_published_at")) or datetime.min.replace(tzinfo=timezone.utc), _score_int(row)),
            reverse=True,
        )
        ready_by_job: dict[str, dict[str, Any]] = {}
        for row in alerts:
            job_id = clean(row.get("job_id"))
            if job_id and job_id not in ready_by_job:
                ready_by_job[job_id] = row
        ready_alerts = list(ready_by_job.values())
        alerts = (
            ready_alerts[:200]
            if scan_current
            else ready_alerts[: config["max_alerts_per_poll"]]
        )
        alerted_keys = {clean(row.get("source_key")) for row in alerts}
        for row in ready_alerts:
            source_key = clean(row.get("source_key"))
            if not source_key:
                continue
            state["sources"][source_key]["decision"] = (
                "ALERTED" if source_key in alerted_keys else "PENDING_READY"
            )

        if successful_boards and not was_initialized:
            state["initialized"] = True
            state["initialized_at"] = stamp
        state["last_poll_started_at"] = stamp
        completed = self.clock().astimezone(timezone.utc)
        state["last_poll_completed_at"] = iso_utc(completed)
        _write_json(self.state_path, state)

        history = _read_json(self.json_path, [])
        if not isinstance(history, list):
            history = []
        by_job_id = {
            clean(row.get("job_id")): row
            for row in history
            if isinstance(row, dict) and clean(row.get("job_id"))
        }
        for row in alerts:
            by_job_id[clean(row.get("job_id"))] = row
        _write_rapid_outputs(list(by_job_id.values()), self.json_path, self.csv_path, self.md_path)

        status = {
            "status": "SUCCESS" if successful_boards else "FAILED",
            "scan_current": scan_current,
            "scan_window_days": scan_window_days if scan_current else None,
            "enabled": config["enabled"],
            "interval_seconds": config["interval_seconds"],
            "started_at": stamp,
            "completed_at": iso_utc(completed),
            "next_poll_due_at": iso_utc(completed + timedelta(seconds=config["interval_seconds"])),
            "initialized_before_poll": was_initialized,
            "boards_monitored": [f"{item['provider']}:{item['board']}" for item in boards],
            "board_count": len(boards),
            "successful_boards": successful_boards,
            "unchanged_boards": unchanged_boards,
            "failed_boards": len(errors),
            "entries_seen": len(entries),
            "new_source_entries": len(new_entries),
            "baseline_entries_added": baseline_count,
            "relevant_new_entries": relevant_count,
            "confirmed_open_entries": open_count,
            "alerts_ready": len(alerts),
            "new_jobs": alerts,
            "errors": errors,
        }
        _write_json(self.status_path, status)
        return status


def status_summary(config_path: Path = CONFIG_PATH, status_path: Path = WATCH_STATUS_PATH) -> dict[str, Any]:
    config = load_watch_config(config_path)
    status = _read_json(status_path, {})
    return {
        "enabled": config["enabled"],
        "interval_seconds": config["interval_seconds"],
        "last_status": status.get("status", "NEVER_RUN") if isinstance(status, dict) else "NEVER_RUN",
        "last_completed_at": status.get("completed_at", "") if isinstance(status, dict) else "",
        "next_poll_due_at": status.get("next_poll_due_at", "") if isinstance(status, dict) else "",
        "board_count": status.get("board_count", 0) if isinstance(status, dict) else 0,
        "entries_seen": status.get("entries_seen", 0) if isinstance(status, dict) else 0,
        "alerts_ready": status.get("alerts_ready", 0) if isinstance(status, dict) else 0,
        "failed_boards": status.get("failed_boards", 0) if isinstance(status, dict) else 0,
    }


def requeue_alert(source_key: str, reason: str = "Telegram delivery failed.", state_path: Path = WATCH_STATE_PATH) -> bool:
    state = _read_json(state_path, {})
    if not isinstance(state, dict) or not isinstance(state.get("sources"), dict):
        return False
    source = state["sources"].get(clean(source_key))
    if not isinstance(source, dict):
        return False
    source["decision"] = "PENDING_READY"
    source["decision_reason"] = clean(reason)
    _write_json(state_path, state)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Rapid public-ATS watcher for new Singapore technical jobs.")
    parser.add_argument("action", choices=["once", "scan", "start", "stop", "status"], nargs="?", default="once")
    args = parser.parse_args()

    if args.action == "start":
        print(json.dumps(update_watch_config({"enabled": True}), indent=2))
        return
    if args.action == "stop":
        print(json.dumps(update_watch_config({"enabled": False}), indent=2))
        return
    if args.action == "status":
        print(json.dumps(status_summary(), indent=2))
        return
    result = RapidJobWatcher().poll(force=True, scan_current=args.action == "scan")
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"new_jobs"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
