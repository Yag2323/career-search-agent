from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


FAMILY_PATTERNS = {
    "AI_ML": [
        r"\bartificial intelligence\b",
        r"\bai\b",
        r"\bml\b",
        r"\bai (?:engineer|engineering|developer|intern|apprentice|research|scientist)\b",
        r"\bmachine learning\b",
        r"\bdeep learning\b",
        r"\bgenerative ai\b",
        r"\blarge language models?\b",
        r"\bllms?\b",
        r"\bdata scientist\b",
    ],
    "COMPUTER_VISION": [
        r"\bcomputer vision\b",
        r"\bimage processing\b",
        r"\bobject detection\b",
        r"\bvisual perception\b",
        r"\bopencv\b",
    ],
    "ROBOTICS_AUTONOMY": [
        r"\brobot(?:ics|ic)?\b",
        r"\bautonomous (?:systems?|vehicles?|robot|navigation)\b",
        r"\bmobile robot\b",
        r"\bslam\b",
        r"\blocali[sz]ation\b",
        r"\bmapping\b",
        r"\bpath planning\b",
    ],
    "EMBEDDED_IOT": [
        r"\bembedded(?: systems?| software)?\b",
        r"\bfirmware\b",
        r"\bmicrocontrollers?\b",
        r"\bstm32\b",
        r"\binternet of things\b",
        r"\biot\b",
        r"\bsensor(?:s| integration)?\b",
        r"\bhardware software integration\b",
    ],
    "AUTOMATION_CONTROLS": [
        r"\bautomation engineer(?:ing)?\b",
        r"\bcontrols? engineer(?:ing)?\b",
        r"\bcontrol systems?\b",
        r"\bmechatronics?\b",
        r"\bindustrial automation\b",
    ],
    "DATA_ANALYTICS": [
        r"\bdata analyst\b",
        r"\bdata engineer(?:ing)?\b",
        r"\bdata science\b",
        r"\banalytics engineer(?:ing)?\b",
    ],
    "SOFTWARE_ENGINEERING": [
        r"\bsoftware engineer(?:ing)?\b",
        r"\bsoftware developer\b",
        r"\bbackend developer\b",
        r"\bfull stack developer\b",
        r"\bpython developer\b",
        r"\bc\+\+ developer\b",
    ],
}

CORE_FAMILIES = {
    "AI_ML",
    "COMPUTER_VISION",
    "ROBOTICS_AUTONOMY",
    "EMBEDDED_IOT",
    "AUTOMATION_CONTROLS",
}

NON_TECHNICAL_TITLE_PATTERNS = [
    r"\baccount(?:ing|ant)\b",
    r"\btax\b",
    r"\bfinance\b",
    r"\bhuman resources?\b",
    r"\bmarketing\b",
    r"\bsales\b",
    r"\bcustomer (?:experience|service|success|support)\b",
    r"\bbusiness development\b",
    r"\bprocurement\b",
    r"\blegal\b",
    r"\baudit\b",
    r"\bproject management\b",
    r"\bproduct management\b",
    r"\bux\s*/?\s*ui\b",
    r"\buxui\b",
    r"\buser experience\b",
    r"\bbusiness(?:\s*&\s*strategy)? analyst\b",
    r"\bstrategy analyst\b",
    r"\bgtm\b",
    r"\bgo[ -]to[ -]market\b",
]

HARD_BUSINESS_TITLE_PATTERNS = [
    r"\bmarketing\b",
    r"\bsales\b",
    r"\bgtm\b",
    r"\bgo[ -]to[ -]market\b",
    r"\bbusiness(?:\s*&\s*strategy)? analyst\b",
    r"\bstrategy analyst\b",
    r"\bproduct management\b",
    r"\bfaculty member\b",
    r"\bprofessor\b",
    r"\blecturer\b",
]

AMBIGUOUS_ENGINEERING_TITLE_PATTERNS = [
    r"\bcompletions?\b",
    r"\bdrilling\b",
    r"\bwell services?\b",
    r"\bfield operations?\b",
    r"\btechnical professional\b",
]

STRONG_CORE_EVIDENCE_PATTERNS = [
    r"\bartificial intelligence\b",
    r"\bmachine learning\b",
    r"\bdeep learning\b",
    r"\bgenerative ai\b",
    r"\blarge language models?\b",
    r"\bllms?\b",
    r"\bcomputer vision\b",
    r"\bmodel (?:training|evaluation|inference|deployment)\b",
    r"\bpytorch\b",
    r"\btensorflow\b",
    r"\brobot(?:ics|ic)?\b",
    r"\bautonomous systems?\b",
    r"\bembedded systems?\b",
    r"\bfirmware\b",
    r"\bmicrocontrollers?\b",
    r"\bcontrol systems?\b",
]

TECHNICAL_OCCUPATION_PATTERNS = [
    r"\b(?:ai|ml|machine learning|computer vision|robotics?|embedded|firmware) (?:engineer|developer|intern|apprentice|researcher|scientist)\b",
    r"\b(?:software|automation|controls?|mechatronics?|data) (?:engineer|developer|analyst|scientist|intern|apprentice)\b",
    r"\bengineering (?:intern|internship|graduate|apprentice|trainee)\b",
    r"\btechnical (?:intern|graduate|apprentice|trainee)\b",
]

SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head|architect|vp|vice president)\b",
    flags=re.IGNORECASE,
)
EXPERIENCED_SOURCE_LEVEL_PATTERN = re.compile(
    r"\b(?:mid[ -]senior|senior|executive|director|manager|leadership)\b",
    flags=re.IGNORECASE,
)


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _families(text: str) -> list[str]:
    return [name for name, patterns in FAMILY_PATTERNS.items() if _matches(text, patterns)]


def role_title(row: dict[str, Any]) -> str:
    title = clean(row.get("title"))
    company = clean(row.get("company"))
    if company:
        title = re.sub(
            rf"\s*(?:[-|]|\bat\b)\s*{re.escape(company)}\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
    return clean(title)


def minimum_experience_years(row: dict[str, Any]) -> int | None:
    text = " ".join([clean(row.get("snippet")), clean(row.get("extracted_content"))])
    values: list[int] = []
    patterns = [
        r"\bat\s+least\s+(?:[a-z]+\s*)?\((\d{1,2})\)\s*(?:years?|yrs?)\b",
        r"\b(\d{1,2})\s*(?:\+|or more|or greater)?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:professional\s+|relevant\s+|hands-on\s+|industry\s+|work\s+){0,2}(?:experience|in\b|working\b|developing\b|building\b)",
        r"\b(\d{1,2})\s*(?:\+|or more|or greater)?\s*(?:years?|yrs?)\b(?=[^.:;\n]{0,90}\bexperience\b)",
        r"\bminimum(?:\s+of)?\s+(\d{1,2})\s*(?:years?|yrs?)\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = int(match.group(1))
            if value <= 15:
                values.append(value)
    return max(values) if values else None


def postgraduate_degree_required(row: dict[str, Any]) -> bool:
    title = role_title(row).lower()
    text = " ".join([title, clean(row.get("snippet")).lower(), clean(row.get("extracted_content")).lower()])
    if re.search(r"\b(?:ph\.?d|doctorate|doctoral)\b", title):
        return True
    patterns = [
        r"\bph\.?d\s+only\b",
        r"\bph\.?d\s+holder\b",
        r"\bph\.?d\s*\(\s*or\s+exceptional\s+m\.?sc\b",
        r"\brequirements?\s*:?[\s-]*(?:a\s+)?(?:master'?s|masters|m\.?sc|ph\.?d|doctoral)\b",
        r"\bminimum(?:\s+qualification)?(?:\s+of)?(?:\s+a)?\s+(?:master'?s|m\.?sc|ph\.?d|doctoral)\b",
        r"\b(?:master'?s|m\.?sc|ph\.?d|doctoral)(?:\s+degree)?\s+(?:is\s+)?required\b",
        r"\bmust\s+have(?:\s+a)?\s+(?:master'?s|m\.?sc|ph\.?d|doctoral)\b",
        r"\b(?:requirements?|must-have qualifications?|minimum qualifications?)\b.{0,120}\b(?:ph\.?d|doctorate|doctoral degree)\b",
    ]
    return _matches(text, patterns)


def experienced_hire_signals(row: dict[str, Any]) -> list[str]:
    text = " ".join([clean(row.get("snippet")), clean(row.get("extracted_content"))]).lower()
    patterns = {
        "mentoring other engineers": [
            r"\bmentor(?:ing)?\s+(?:and\s+review\s+)?(?:work\s+from\s+)?other\s+(?:ml\s+)?engineers\b",
            r"\btechnical guidance and mentorship\b",
        ],
        "ownership of production systems": [
            r"\bown\s+(?:critical\s+)?(?:ml\s+)?(?:systems?|subsystems?|services?)\s+in\s+production\b",
            r"\bindependent ownership\b",
        ],
        "prior production delivery": [
            r"\bexperience\s+building\s+and\s+shipping\s+.+?\b(?:used by real users|in production)\b",
            r"\bhands-on experience\s+taking\s+.+?\bthrough\s+deployment\s+and\s+(?:ongoing\s+)?operation\s+in\s+production\b",
        ],
        "project or customer leadership": [
            r"\b(?:manage|lead)\s+projects?\s+and\s+lead\s+them\s+to\s+successful completion\b",
            r"\blead qualification calls\b",
        ],
        "established implementation experience": [
            r"\bproven experience\s+in\s+(?:product\s+)?implementation\b",
            r"\bconsulting experience\b",
        ],
        "solution architecture ownership": [
            r"\bsolution architecture\b",
            r"\bdesign and provision\s+(?:cloud\s+)?environments?\b",
        ],
        "executive stakeholder leadership": [
            r"\bexecutive-level relationships\b",
            r"\b(?:c-level|senior executive|senior stakeholders?)\b",
        ],
        "people leadership": [
            r"\bhire,?\s+coach,?\s+and\s+retain\b",
            r"\blead\s+(?:cross-functional|multi-disciplinary|multidisciplinary)\s+teams?\b",
        ],
        "architecture ownership": [
            r"\barchitect\s+and\s+develop\s+reusable,?\s+scalable\b",
            r"\bown\s+the\s+end-to-end\s+build\b",
        ],
        "technical leadership": [
            r"\btechnical leadership and mentorship\b",
            r"\bmentor(?:ship|ing)?\s+(?:to\s+)?junior engineers?\b",
            r"\boversee\s+the\s+deployment\b",
        ],
        "large-scale platform ownership": [
            r"\bhundreds of thousands to millions\b",
            r"\bexperience building systems that scale to high user volumes\b",
        ],
    }
    return [label for label, expressions in patterns.items() if _matches(text, expressions)]


def career_stage(row: dict[str, Any]) -> tuple[str, int]:
    title = role_title(row).lower()
    body = " ".join([clean(row.get("snippet")).lower(), clean(row.get("extracted_content")).lower()])
    source_level = clean(row.get("source_experience_level")).lower()
    if EXPERIENCED_SOURCE_LEVEL_PATTERN.search(source_level):
        return "SENIOR", 0
    if re.search(r"\b(?:intern|internship)\b", source_level):
        return "INTERNSHIP", 95
    if re.search(r"\b(?:entry[ -]level|associate)\b", source_level):
        return "ENTRY_LEVEL", 90
    if re.search(r"\bapprentice(?:ship)?\b", title):
        return "APPRENTICESHIP", 100
    if re.search(r"\b(?:fresh|new) grad(?:uate)?\b|\bgraduate (?:programme|program|engineer|developer|role)\b", title):
        return "FRESH_GRADUATE", 100
    if re.search(r"\bintern(?:ship)?\b", title):
        return "INTERNSHIP", 95
    if re.search(r"\b(?:entry[ -]level|early careers?)\b", title):
        return "ENTRY_LEVEL", 90
    if re.search(r"\b(?:junior|associate engineer|trainee)\b", title):
        return "JUNIOR_OR_TRAINEE", 85
    if SENIOR_TITLE_PATTERN.search(title):
        return "SENIOR", 0
    if re.search(r"\b(?:fresh|new) grad(?:uate)?\b|\bgraduate (?:programme|program|engineer|developer|role)\b", body):
        return "FRESH_GRADUATE", 95
    if re.search(r"\bapprentice(?:ship)?\b", body):
        return "APPRENTICESHIP", 95
    if re.search(r"\b(?:entry[ -]level|early careers?)\b", body):
        return "ENTRY_LEVEL", 85
    return "EARLY_CAREER_UNSPECIFIED", 55


def degree_fit(row: dict[str, Any]) -> dict[str, str]:
    title = role_title(row).lower()
    description = " ".join(
        [clean(row.get("snippet")).lower(), clean(row.get("extracted_content")).lower()]
    )
    body = " ".join([title, description])
    title_families = _families(title)
    body_families = _families(body)
    core_title = [item for item in title_families if item in CORE_FAMILIES]
    core_body = [item for item in body_families if item in CORE_FAMILIES]
    technical_occupation = _matches(title, TECHNICAL_OCCUPATION_PATTERNS)
    non_technical_title = _matches(title, NON_TECHNICAL_TITLE_PATTERNS)
    hard_business_title = _matches(title, HARD_BUSINESS_TITLE_PATTERNS)

    ambiguous_engineering_title = _matches(title, AMBIGUOUS_ENGINEERING_TITLE_PATTERNS)
    supported_core_work = _matches(description, STRONG_CORE_EVIDENCE_PATTERNS)

    if hard_business_title:
        score = 0
        level = "UNRELATED"
        reason = "The title is a business, sales, or marketing role rather than an engineering role for this degree."
    elif non_technical_title and not technical_occupation:
        score = 0
        level = "UNRELATED"
        reason = "The title is business-facing rather than an AI, robotics, embedded, data, or software engineering role."
    elif ambiguous_engineering_title and not supported_core_work:
        score = 0
        level = "UNRELATED"
        reason = "The engineering title belongs to another discipline and the description contains no verified AI, robotics, embedded, or controls work."
    elif core_title:
        score = min(100, 88 + 4 * len(core_title))
        level = "CORE_DEGREE_MATCH"
        reason = "The title directly matches the AI and Robotics degree."
    elif any(item in title_families for item in {"DATA_ANALYTICS", "SOFTWARE_ENGINEERING"}):
        score = 72 if core_body else 62
        level = "ADJACENT_TECHNICAL_MATCH"
        reason = "The role is a relevant software or data pathway supported by the degree and project portfolio."
    elif technical_occupation and core_body:
        score = 78
        level = "CORE_DEGREE_MATCH"
        reason = "The technical role description directly uses AI, robotics, embedded, or automation skills."
    elif re.search(r"\b(?:technology|engineering) (?:graduate|apprentice|trainee|intern)\b", title) and core_body:
        score = 74
        level = "ADJACENT_TECHNICAL_MATCH"
        reason = "The early-career engineering pathway contains degree-related technical work."
    else:
        score = 0
        level = "UNRELATED"
        reason = "No sufficiently specific AI, robotics, embedded, automation, data, or software engineering role signal was found."

    return {
        "degree_fit_level": level,
        "degree_fit_score": str(score),
        "degree_families": "; ".join(body_families),
        "degree_fit_reason": reason,
    }


def parse_published_at(value: Any, now: datetime | None = None) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)) or text.isdigit():
        try:
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(current.tzinfo)
        except (ValueError, OSError, OverflowError):
            return None

    lower = text.lower()
    if lower in {"now", "just now", "today"}:
        return current
    if lower == "yesterday":
        return current - timedelta(days=1)
    relative = re.search(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\s*ago\b", lower)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith(("minute", "min")):
            return current - timedelta(minutes=amount)
        if unit.startswith(("hour", "hr")):
            return current - timedelta(hours=amount)
        if unit.startswith("day"):
            return current - timedelta(days=amount)
        return current - timedelta(weeks=amount)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=current.tzinfo)
        return parsed.astimezone(current.tzinfo)
    except ValueError:
        pass

    for fmt in [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
    ]:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=current.tzinfo)
        except ValueError:
            continue
    return None


def page_published_hint(row: dict[str, Any]) -> str:
    text = " ".join([clean(row.get("snippet")), clean(row.get("extracted_content"))])[:20000]
    token = (
        r"just now|today|yesterday|\d+\s*(?:minutes?|mins?|hours?|hrs?|days?)\s*ago|"
        r"\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}/\d{1,2}/\d{4}|"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}|"
        r"\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}"
    )
    patterns = [
        rf"\bposting\s+start\s+date\s*[:\-]?\s*({token})\b",
        rf"\b(?:posted|published|listed)\s*(?:on\s*)?[:\-]?\s*({token})\b",
        rf"\bdate\s*:\s*({token})\b",
        rf"\bapply\s+({token})\b",
        rf"\bsingapore\s+({token})\s+until\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean(match.group(1))
    return ""


def recency_fields(row: dict[str, Any], now: datetime | None = None) -> dict[str, str]:
    current = now or datetime.now().astimezone()
    raw = clean(row.get("source_published_at_raw") or row.get("source_published_at"))
    published = parse_published_at(raw, current)
    prior_evidence = clean(row.get("recency_evidence")).upper()
    evidence = (
        prior_evidence
        if prior_evidence in {"PROVIDER_PUBLISHED_TIMESTAMP", "PAGE_POSTING_TIMESTAMP"}
        else "PROVIDER_PUBLISHED_TIMESTAMP"
    )
    if clean(row.get("ats_provider")).upper() == "SUCCESSFACTORS_RSS":
        explicit_page_date = page_published_hint(row)
        explicit_published = parse_published_at(explicit_page_date, current)
        if explicit_published:
            raw = explicit_page_date
            published = explicit_published
            evidence = "PAGE_POSTING_TIMESTAMP"
    if not published:
        inferred = page_published_hint(row)
        inferred_published = parse_published_at(inferred, current)
        if inferred_published:
            raw = inferred
            published = inferred_published
            evidence = "PAGE_POSTING_TIMESTAMP"
    updated = None
    if not published:
        updated = parse_published_at(row.get("source_updated_at"), current)
        if updated:
            published = updated
            evidence = "ATS_UPDATED_TIMESTAMP"
    window = clean(row.get("search_time_range")).lower()

    if published:
        age_hours = max(0.0, (current - published).total_seconds() / 3600)
        prefix = "UPDATED" if updated else "POSTED"
        if age_hours <= 24:
            status, score = f"{prefix}_WITHIN_24_HOURS", 100
        elif age_hours <= 72:
            status, score = f"{prefix}_WITHIN_3_DAYS", 80
        elif age_hours <= 168:
            status, score = f"{prefix}_WITHIN_7_DAYS", 60
        elif age_hours <= 336:
            status, score = f"{prefix}_WITHIN_14_DAYS", 45
        elif age_hours <= 720:
            status, score = f"{prefix}_WITHIN_30_DAYS", 30
        elif age_hours <= 1440:
            status, score = f"{prefix}_WITHIN_60_DAYS", 15
        elif age_hours <= 2160:
            status, score = f"{prefix}_WITHIN_90_DAYS", 5
        elif age_hours <= 2880:
            status, score = f"{prefix}_WITHIN_120_DAYS", 1
        else:
            status, score = "UPDATED_OLDER_THAN_120_DAYS" if updated else "OLDER_THAN_120_DAYS", 0
        return {
            "source_published_at_raw": raw,
            "source_published_at": "" if updated else published.isoformat(timespec="seconds"),
            "posting_age_hours": f"{age_hours:.1f}",
            "recency_status": status,
            "recency_score": str(score),
            "recency_evidence": evidence,
        }

    detected = parse_published_at(row.get("detected_at"), current)
    if clean(row.get("source_query")).upper() == "RAPID_DIRECT_ATS_WATCH" and detected:
        detected_age = max(0.0, (current - detected).total_seconds() / 3600)
        if detected_age <= 24:
            status, score, evidence = "NEWLY_DETECTED_BY_ATS_WATCH", 85, "DIRECT_ATS_FIRST_SEEN_TIMESTAMP"
        else:
            status, score, evidence = "POSTING_TIME_UNKNOWN", 20, "NO_PUBLISH_TIMESTAMP"
    elif window in {"day", "d"}:
        status, score, evidence = "SURFACED_IN_24H_SEARCH", 70, "SEARCH_WINDOW_24H_TIMESTAMP_UNAVAILABLE"
    elif window in {"week", "w"}:
        status, score, evidence = "SURFACED_IN_7D_SEARCH", 45, "SEARCH_WINDOW_7D_TIMESTAMP_UNAVAILABLE"
    elif window in {"month", "m"}:
        status, score, evidence = "SURFACED_IN_30D_SEARCH", 25, "SEARCH_WINDOW_30D_TIMESTAMP_UNAVAILABLE"
    else:
        status, score, evidence = "POSTING_TIME_UNKNOWN", 20, "NO_PUBLISH_TIMESTAMP"
    return {
        "source_published_at_raw": raw,
        "source_published_at": "",
        "posting_age_hours": "",
        "recency_status": status,
        "recency_score": str(score),
        "recency_evidence": evidence,
    }


def enrich_targeting(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    output = dict(row)
    output.update(degree_fit(output))
    stage, stage_score = career_stage(output)
    output["career_stage"] = stage
    output["career_stage_score"] = str(stage_score)
    experience_years = minimum_experience_years(output)
    output["minimum_experience_years"] = "" if experience_years is None else str(experience_years)
    output.update(recency_fields(output, now))
    return output


def is_target_degree_role(row: dict[str, Any]) -> tuple[bool, str]:
    enriched = enrich_targeting(row)
    title = clean(enriched.get("title"))
    if postgraduate_degree_required(enriched):
        return False, "Requires a postgraduate degree beyond the confirmed bachelor's profile."
    if SENIOR_TITLE_PATTERN.search(title) and clean(enriched.get("career_stage")) == "SENIOR":
        return False, "Senior-level title"
    source_level = clean(enriched.get("source_experience_level"))
    if EXPERIENCED_SOURCE_LEVEL_PATTERN.search(source_level):
        return False, f"ATS experience level is {source_level}."
    if int(enriched.get("degree_fit_score") or 0) < 55:
        return False, clean(enriched.get("degree_fit_reason"))
    experience_years = minimum_experience_years(enriched)
    if experience_years is not None and experience_years >= 2:
        return False, f"Requires {experience_years} years of experience, which is not a fresh-graduate role."
    experience_signals = experienced_hire_signals(enriched)
    if clean(enriched.get("career_stage")) == "EARLY_CAREER_UNSPECIFIED" and len(experience_signals) >= 2:
        return False, "Experienced-hire expectations: " + ", ".join(experience_signals) + "."
    return True, clean(enriched.get("degree_fit_reason"))


def recency_rank(row: dict[str, Any]) -> int:
    return {
        "POSTED_WITHIN_24_HOURS": 5,
        "UPDATED_WITHIN_24_HOURS": 5,
        "SURFACED_IN_24H_SEARCH": 4,
        "NEWLY_DETECTED_BY_ATS_WATCH": 4,
        "POSTED_WITHIN_3_DAYS": 3,
        "UPDATED_WITHIN_3_DAYS": 3,
        "POSTED_WITHIN_7_DAYS": 2,
        "UPDATED_WITHIN_7_DAYS": 2,
        "SURFACED_IN_7D_SEARCH": 1,
        "POSTED_WITHIN_14_DAYS": 1,
        "UPDATED_WITHIN_14_DAYS": 1,
        "POSTED_WITHIN_30_DAYS": 0,
        "UPDATED_WITHIN_30_DAYS": 0,
        "SURFACED_IN_30D_SEARCH": 0,
        "POSTING_TIME_UNKNOWN": 0,
        "POSTED_WITHIN_60_DAYS": -1,
        "UPDATED_WITHIN_60_DAYS": -1,
        "POSTED_WITHIN_90_DAYS": -2,
        "UPDATED_WITHIN_90_DAYS": -2,
        "POSTED_WITHIN_120_DAYS": -3,
        "UPDATED_WITHIN_120_DAYS": -3,
        "OLDER_THAN_30_DAYS": -1,
        "UPDATED_OLDER_THAN_30_DAYS": -1,
        "OLDER_THAN_60_DAYS": -2,
        "UPDATED_OLDER_THAN_60_DAYS": -2,
        "OLDER_THAN_90_DAYS": -3,
        "UPDATED_OLDER_THAN_90_DAYS": -3,
        "OLDER_THAN_120_DAYS": -4,
        "UPDATED_OLDER_THAN_120_DAYS": -4,
        "OLDER_THAN_7_DAYS": -1,
        "UPDATED_OLDER_THAN_7_DAYS": -1,
    }.get(clean(row.get("recency_status")).upper(), 0)


def posting_age_rank(row: dict[str, Any]) -> float:
    try:
        age_hours = float(clean(row.get("posting_age_hours")))
        return -age_hours if age_hours >= 0 else -1_000_000.0
    except (TypeError, ValueError):
        return -1_000_000.0


def recent_enough_for_digest(row: dict[str, Any]) -> bool:
    return clean(row.get("recency_status")).upper() not in {
        "OLDER_THAN_120_DAYS",
        "UPDATED_OLDER_THAN_120_DAYS",
        "OLDER_THAN_7_DAYS",
        "UPDATED_OLDER_THAN_7_DAYS",
    }


def career_stage_rank(row: dict[str, Any]) -> int:
    return {
        "FRESH_GRADUATE": 6,
        "APPRENTICESHIP": 5,
        "ENTRY_LEVEL": 4,
        "JUNIOR_OR_TRAINEE": 4,
        "INTERNSHIP": 3,
        "EARLY_CAREER_UNSPECIFIED": 2,
        "SENIOR": 0,
    }.get(clean(row.get("career_stage")).upper(), 1)
