from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from application_capability import application_capability
from career_agent_common import STATE_DIR, clean
from career_targeting import enrich_targeting, is_target_degree_role, minimum_experience_years
from eligibility import assess_eligibility
from job_identity import domain_of, source_type
from requirements_matcher import enrich_job_with_requirements, match_requirements

WEIGHTS_PATH = STATE_DIR / "scoring_weights.json"

BASE_WEIGHTS = {
    "location": 0.10,
    "eligibility": 0.14,
    "level": 0.14,
    "relevance": 0.20,
    "recency": 0.12,
    "direct": 0.10,
    "salary": 0.04,
    "deadline": 0.04,
    "honest": 0.12,
}

AGGREGATOR_DOMAINS = [
    "adzuna.sg",
    "aijobs.net",
    "builtinsingapore.com",
    "facebook.com",
    "foundit.sg",
    "grabjobs.co",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "glassdoor.sg",
    "glints.com",
    "jobstreet.com",
    "jobstreet.com.sg",
    "internsg.com",
    "jobleads.com",
    "jobsdb.com",
    "prosple.com",
    "pitchmeai.com",
    "jora.com",
    "jobcube.sg",
    "remoterocketship.com",
    "talent.com",
    "wellfound.com",
]

MANUAL_REVIEW_DOMAINS = ["mycareersfuture.gov.sg"]
RISK_DOMAINS = AGGREGATOR_DOMAINS + MANUAL_REVIEW_DOMAINS

SCAM_TERMS = [
    "transfer money",
    "advance payment",
    "pay a fee",
    "training fee",
    "registration fee",
    "use your bank account",
    "bank login",
    "singpass password",
    "singpass qr",
    "crypto payment",
    "commission for simple tasks",
]


def row_text(row: dict[str, Any]) -> str:
    return " ".join(
        clean(row.get(key))
        for key in [
            "company",
            "title",
            "location",
            "remote_type",
            "link",
            "snippet",
            "extracted_content",
        ]
    )


def contains_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term in lower for term in terms)


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    if not path.exists():
        return dict(BASE_WEIGHTS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidate = payload.get("weights", payload)
        weights = {key: float(candidate.get(key, BASE_WEIGHTS[key])) for key in BASE_WEIGHTS}
        total = sum(max(0.0, item) for item in weights.values())
        if total <= 0:
            return dict(BASE_WEIGHTS)
        return {key: max(0.0, value) / total for key, value in weights.items()}
    except Exception:
        return dict(BASE_WEIGHTS)


def location_score(row: dict[str, Any]) -> tuple[int, list[str]]:
    text = row_text(row).lower()
    reasons: list[str] = []
    score = 0
    if "singapore" in text:
        score += 60
        reasons.append("Singapore is explicitly mentioned")
    if ".sg" in clean(row.get("link")).lower() or "mycareersfuture" in text:
        score += 20
        reasons.append("Singapore source/domain")
    if "remote" in text and any(term in text for term in ["singapore", "apac", "asia"]):
        score += 15
        reasons.append("Remote or hybrid wording appears compatible with Singapore")
    if "relocation" in text and "singapore" not in text:
        score -= 30
        reasons.append("Relocation ambiguity")
    return max(0, min(100, score)), reasons


def level_score(row: dict[str, Any]) -> tuple[int, list[str]]:
    stage = clean(row.get("career_stage")).upper()
    if not stage:
        stage = clean(enrich_targeting(row).get("career_stage")).upper()
    reasons: list[str] = []
    if stage == "FRESH_GRADUATE":
        score = 100
        reasons.append("Fresh graduate fit")
    elif stage == "APPRENTICESHIP":
        score = 100
        reasons.append("Apprenticeship fit")
    elif stage == "ENTRY_LEVEL":
        score = 90
        reasons.append("Entry-level fit")
    elif stage == "JUNIOR_OR_TRAINEE":
        score = 85
        reasons.append("Junior or trainee fit")
    elif stage == "INTERNSHIP":
        score = 70
        reasons.append("Internship or apprenticeship fit")
    elif stage == "SENIOR":
        score = 0
        reasons.append("Senior-level signal")
    else:
        score = 35
        reasons.append("Career level is not explicitly stated")
    years = minimum_experience_years(row)
    if years is not None and years >= 3:
        score -= min(60, years * 10)
        reasons.append(f"Requires {years} years of experience")
    return max(0, min(100, score)), reasons


def relevance_score(row: dict[str, Any], requirements: dict[str, Any]) -> tuple[int, list[str]]:
    matched_categories = [item for item in clean(row.get("degree_families")).split("; ") if item]
    degree_score = int(row.get("degree_fit_score") or 0)
    requirement_total = max(1, int(requirements.get("requirement_count", 0)))
    evidence_coverage = 100 * int(requirements.get("matched_count", 0)) / requirement_total
    score = round(degree_score * 0.70 + evidence_coverage * 0.30)
    return max(0, min(100, score)), matched_categories


def classify_apply_type(row: dict[str, Any]) -> tuple[str, list[str]]:
    text = row_text(row).lower()
    url = clean(row.get("canonical_url") or row.get("link"))
    surface = " ".join(
        [clean(row.get("title")), clean(row.get("link")), clean(row.get("snippet"))[:500]]
    ).lower()
    detected = source_type(url)
    availability = clean(row.get("availability_status")).upper()
    if availability == "CLOSED":
        return "SKIP", ["Live vacancy check says the listing is closed or expired"]
    if clean(row.get("career_stage")).upper() == "SENIOR":
        return "SKIP", ["ATS metadata or role evidence identifies an experienced-hire vacancy"]
    if not location_score(row)[0]:
        return "SKIP", ["Not clearly Singapore-compatible"]
    if contains_any(surface, ["course", "bootcamp", "salary guide", "interview questions", "resume example"]):
        return "SKIP", ["Not a job application page"]
    if contains_any(
        text,
        [
            "don't see the right fit in our actively posted roles",
            "do not see the right fit in our actively posted roles",
            "you should not expect to hear back from us",
            "join our talent community",
            "general application for future opportunities",
            "expression of interest for future opportunities",
        ],
    ):
        return "SKIP", ["Evergreen talent pool, not a specific open vacancy"]
    if contains_any(surface, ["senior", "lead ", "principal", "manager"]) and not contains_any(
        surface, ["intern", "junior", "graduate"]
    ):
        return "SKIP", ["Senior role without an early-career signal"]
    if detected == "AGGREGATOR_OR_MANUAL":
        domain = domain_of(url)
        if any(item in domain for item in MANUAL_REVIEW_DOMAINS):
            return "MANUAL_REVIEW", ["Government/manual-review portal"]
        return "AGGREGATOR_ONLY", ["Aggregator or login-heavy job board"]
    if detected in {"DIRECT_APPLY", "COMPANY_CAREER_PAGE"}:
        if availability == "UNVERIFIED":
            return "MANUAL_REVIEW", ["Direct page exists, but live availability could not be verified"]
        return "DIRECT_APPLY", ["Direct employer or ATS page"]
    return "MANUAL_REVIEW", ["Employer source needs manual verification"]


def direct_score(apply_type: str) -> int:
    return {"DIRECT_APPLY": 100, "MANUAL_REVIEW": 60, "AGGREGATOR_ONLY": 30, "SKIP": 0}.get(
        apply_type, 40
    )


def extract_salary(row: dict[str, Any]) -> tuple[str, int]:
    text = row_text(row)
    patterns = [
        r"S\$\s?[\d,]+(?:\s*(?:-|–|—|to)\s*S?\$?\s?[\d,]+)?(?:\s*(?:per month|/month|monthly|pm))?",
        r"SGD\s?[\d,]+(?:\s*(?:-|–|—|to)\s*(?:SGD)?\s?[\d,]+)?(?:\s*(?:per month|/month|monthly|pm))?",
        r"\$\s?[\d,]+(?:\s*(?:-|–|—|to)\s*\$?\s?[\d,]+)?\s*(?:per month|/month|monthly|pm)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean(match.group(0)), 85
    return "", 35


def parse_deadline(value: str) -> datetime | None:
    for fmt in [
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
    ]:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def extract_deadline(row: dict[str, Any]) -> tuple[str, int]:
    text = row_text(row)
    patterns = [
        r"Apply Before\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})",
        r"Apply by\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        r"closing date[:\s]+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        r"deadline[:\s]+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        r"deadline[:\s]+(\d{4}-\d{2}-\d{2})",
        r"closing on\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw = clean(match.group(1))
        parsed = parse_deadline(raw)
        if not parsed:
            return raw, 65
        days = (parsed.date() - datetime.now().date()).days
        if days < 0:
            return raw, 0
        if days <= 7:
            return raw, 100
        if days <= 30:
            return raw, 85
        return raw, 70
    return "", 45


def honest_fit_score(row: dict[str, Any], requirements: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    matched = int(requirements.get("matched_count", 0))
    missing = int(requirements.get("missing_count", 0))
    unknown = int(requirements.get("unknown_count", 0))
    total = matched + missing + unknown
    score = 55 if not total else round(100 * matched / total)
    if matched:
        reasons.append(f"{matched} requirements have verified resume evidence")
    if missing:
        reasons.append(f"{missing} requirements are not supported by the current profile")
        score -= min(35, missing * 7)
    if unknown:
        reasons.append(f"{unknown} requirements need clarification or stronger evidence")
        score -= min(20, unknown * 3)
    if contains_any(row_text(row), ["intern", "graduate", "junior", "entry level"]):
        score += 10
        reasons.append("Role level matches the early-career profile")
    return max(0, min(100, score)), reasons


def risk_flags(
    row: dict[str, Any],
    apply_type: str,
    salary_text: str,
    deadline_text: str,
    eligibility: dict[str, Any],
) -> list[str]:
    text = row_text(row).lower()
    safety_boilerplate_patterns = [
        r"government officials? will never ask.{0,220}?(?:phone call|scamshield|1799)",
        r"mom officers? will never ask.{0,220}?(?:phone call|scamshield|1799)",
        r"do not (?:transfer money|disclose bank).{0,160}",
    ]
    risk_text = text
    for pattern in safety_boilerplate_patterns:
        risk_text = re.sub(pattern, " ", risk_text, flags=re.IGNORECASE)
    domain = domain_of(clean(row.get("link")))
    flags: list[str] = []
    if any(item in domain for item in RISK_DOMAINS):
        flags.append("manual_or_login_heavy_site")
    if contains_any(text, ["captcha", "sign in", "login", "create account"]):
        flags.append("login_or_captcha_possible")
    if contains_any(
        risk_text,
        [
            "passport number",
            "provide passport",
            "nric number",
            "provide nric",
            "government id number",
            "tax identification",
            "bank account details",
            "banking details",
            "singpass password",
            "singpass qr",
        ],
    ):
        flags.append("sensitive_fields_possible")
    if any(term in risk_text for term in SCAM_TERMS):
        flags.append("scam_signal_detected")
    if not salary_text:
        flags.append("salary_missing")
    if not deadline_text:
        flags.append("deadline_missing")
    if contains_any(text, ["citizenship", "permanent resident", "work authorization", "sponsorship", "work pass"]):
        flags.append("work_authorization_question_possible")
    if eligibility["eligibility_status"] == "ELIGIBILITY_UNKNOWN":
        flags.append("eligibility_unknown")
    if eligibility["eligibility_status"] == "INELIGIBLE":
        flags.append("eligibility_blocked")
    if clean(row.get("recency_status")).upper() == "OLDER_THAN_7_DAYS":
        flags.append("posting_older_than_7_days")
    if clean(row.get("recency_evidence")).upper() in {"NO_PUBLISH_TIMESTAMP", "SEARCH_WINDOW_24H_TIMESTAMP_UNAVAILABLE", "SEARCH_WINDOW_7D_TIMESTAMP_UNAVAILABLE"}:
        flags.append("posting_timestamp_unavailable")
    if apply_type == "SKIP":
        flags.append("skip_recommended")
    return list(dict.fromkeys(flags))


def portfolio_match(row: dict[str, Any]) -> tuple[str, str]:
    text = row_text(row).lower()
    if contains_any(text, ["computer vision", "image", "opencv", "object detection", "vision"]):
        return (
            "Visual Safety Monitor",
            "Verified evidence for Python, OpenCV, YOLO, FaceNet, MediaPipe, PyTorch, real-time vision, and documented limitations.",
        )
    if contains_any(text, ["robot", "robotics", "autonomous", "mobile robot", "localization", "mapping", "calibration"]):
        return (
            "Autonomous Mobile Robot & Webots E-puck Autonomy",
            "Verified evidence for embedded sensing, differential-drive control, obstacle response, simulated autonomy, and behaviour testing.",
        )
    if contains_any(text, ["embedded", "iot", "sensor", "actuator", "stm32", "serial", "hardware", "firmware"]):
        return (
            "STM32 + UWP Building Monitor",
            "Verified evidence for STM32 firmware, sensors, serial telemetry, UWP integration, SQLite, testing, and debugging.",
        )
    if contains_any(text, ["machine learning", "data science", "regression", "predictive", "xgboost", "random forest", "model evaluation"]):
        return (
            "Singapore HDB Resale Price Modelling",
            "Verified evidence for data preparation, feature engineering, regression-model comparison, evaluation metrics, and limitation reporting.",
        )
    return (
        "STM32 + UWP Building Monitor",
        "Strong general engineering evidence for end-to-end implementation, testing, debugging, data handling, and technical documentation.",
    )


def score_job(row: dict[str, Any]) -> dict[str, str]:
    enriched = enrich_targeting(enrich_job_with_requirements(row))
    requirements = match_requirements(enriched)
    eligibility = assess_eligibility(enriched)
    apply_type, apply_reasons = classify_apply_type(enriched)
    if eligibility["eligibility_status"] == "INELIGIBLE":
        apply_type = "SKIP"
        apply_reasons.append("Confirmed eligibility conflict")
    elif eligibility["eligibility_status"] == "ELIGIBILITY_UNKNOWN" and apply_type == "DIRECT_APPLY":
        apply_type = "MANUAL_REVIEW"
        apply_reasons.append("Eligibility must be confirmed before direct application")
    hard_missing = [
        item
        for item in requirements.get("missing_requirements", [])
        if "master's or phd" in item.lower() or "years experience" in item.lower()
    ]
    if hard_missing:
        apply_type = "SKIP"
        apply_reasons.append("Hard requirement is not met: " + ", ".join(hard_missing))
    if int(enriched.get("degree_fit_score") or 0) < 55:
        apply_type = "SKIP"
        apply_reasons.append(clean(enriched.get("degree_fit_reason")) or "Role is not sufficiently related to the AI and Robotics degree")
    target_role, target_reason = is_target_degree_role(enriched)
    if not target_role:
        apply_type = "SKIP"
        apply_reasons.append(target_reason)

    location, location_reasons = location_score(enriched)
    level, level_reasons = level_score(enriched)
    relevance, relevance_reasons = relevance_score(enriched, requirements)
    salary_text, salary = extract_salary(enriched)
    deadline_text, deadline = extract_deadline(enriched)
    if deadline_text and deadline == 0:
        apply_type = "SKIP"
        apply_reasons.append(f"Application deadline has passed: {deadline_text}")
    honest, honest_reasons = honest_fit_score(enriched, requirements)
    direct = direct_score(apply_type)
    recency = int(enriched.get("recency_score") or 0)
    flags = risk_flags(enriched, apply_type, salary_text, deadline_text, eligibility)
    project, project_reason = portfolio_match(enriched)
    weights = load_weights()
    capability = application_capability(enriched, apply_type)

    if apply_type == "SKIP":
        quality = 0
    else:
        components = {
            "location": location,
            "eligibility": int(eligibility["eligibility_score"]),
            "level": level,
            "relevance": relevance,
            "recency": recency,
            "direct": direct,
            "salary": salary,
            "deadline": deadline,
            "honest": honest,
        }
        quality = round(sum(components[key] * weights[key] for key in BASE_WEIGHTS))
        if eligibility["eligibility_status"] == "ELIGIBILITY_UNKNOWN":
            quality = min(quality, 74)
        if int(enriched.get("extraction_confidence") or 0) < 45:
            quality = min(quality, 79)

    reasons: list[str] = []
    reasons.extend(apply_reasons)
    reasons.extend(location_reasons[:2])
    reasons.extend(level_reasons[:2])
    reasons.extend(eligibility["eligibility_reasons"][:3])
    if relevance_reasons:
        reasons.append("Relevant families: " + ", ".join(relevance_reasons))
    reasons.append(clean(enriched.get("degree_fit_reason")))
    reasons.append(
        "Posting recency: "
        + clean(enriched.get("recency_status")).replace("_", " ").lower()
        + f" ({clean(enriched.get('recency_evidence'))})"
    )
    reasons.extend(honest_reasons[:3])

    return {
        **enriched,
        **capability,
        "apply_type": apply_type,
        "quality_score": str(max(0, min(100, quality))),
        "singapore_score": str(location),
        "eligibility_status": str(eligibility["eligibility_status"]),
        "eligibility_score": str(eligibility["eligibility_score"]),
        "eligibility_reasons": "; ".join(eligibility["eligibility_reasons"]),
        "eligibility_unknown_fields": "; ".join(eligibility["eligibility_unknown_fields"]),
        "level_score": str(level),
        "relevance_score": str(relevance),
        "recency_score": str(recency),
        "direct_company_score": str(direct),
        "salary_score": str(salary),
        "deadline_score": str(deadline),
        "honest_fit_score": str(honest),
        "salary_text": salary_text,
        "application_deadline": deadline_text,
        "risk_flags": "; ".join(flags),
        "portfolio_project": project,
        "portfolio_reason": project_reason,
        "quality_reasons": "; ".join(dict.fromkeys(reasons)),
        "scoring_weights": json.dumps(weights, sort_keys=True),
    }
