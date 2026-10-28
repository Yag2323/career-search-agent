from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_PATH = BASE / "profile" / "eligibility_profile.json"
EXAMPLE_PROFILE_PATH = BASE / "profile" / "eligibility_profile.example.json"
NO_PASS_WORK_STATUSES = {
    "SINGAPORE CITIZEN",
    "CITIZEN",
    "SINGAPORE PR",
    "PR",
    "PERMANENT RESIDENT",
}


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def load_eligibility_profile(path: Path | str = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    target = Path(path)
    if not target.exists() and target == DEFAULT_PROFILE_PATH and EXAMPLE_PROFILE_PATH.exists():
        target = EXAMPLE_PROFILE_PATH
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def field_value(profile: dict[str, Any], key: str) -> str:
    field = profile.get(key, {})
    if isinstance(field, dict):
        return clean(field.get("value")).upper()
    return clean(field).upper()


def field_confirmation(profile: dict[str, Any], key: str) -> str:
    field = profile.get(key, {})
    if isinstance(field, dict):
        return clean(field.get("confirmation")).upper()
    return ""


def is_unknown(profile: dict[str, Any], key: str) -> bool:
    value = field_value(profile, key)
    confirmation = field_confirmation(profile, key)
    return not value or value == "UNKNOWN" or "REQUIRED" in confirmation


def job_text(row: dict[str, Any]) -> str:
    return " ".join(
        clean(row.get(key))
        for key in ["company", "title", "location", "snippet", "extracted_content"]
    ).lower()


def assess_eligibility(
    row: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = profile if profile is not None else load_eligibility_profile()
    text = job_text(row)
    title = clean(row.get("title")).lower()
    reasons: list[str] = []
    unknown_fields: list[str] = []
    blockers: list[str] = []

    singapore_location = "singapore" in text or ".sg" in clean(row.get("link")).lower()
    remote_from_singapore = "remote" in text and any(
        phrase in text for phrase in ["singapore", "apac", "asia", "work from anywhere"]
    )
    if not singapore_location and not remote_from_singapore:
        blockers.append("Role is not clearly based in Singapore or remote from Singapore.")

    work_status = field_value(profile, "work_status")
    sponsorship = field_value(profile, "sponsorship_required")
    if work_status in NO_PASS_WORK_STATUSES and is_unknown(profile, "sponsorship_required"):
        sponsorship = "NO"
    if is_unknown(profile, "work_status"):
        unknown_fields.append("work_status")
    if work_status not in NO_PASS_WORK_STATUSES and is_unknown(profile, "sponsorship_required"):
        unknown_fields.append("sponsorship_required")

    internship_role = any(term in title for term in ["intern", "internship", "trainee"])
    if internship_role:
        for key in ["current_student", "internship_eligible"]:
            if is_unknown(profile, key):
                unknown_fields.append(key)

    citizen_pr_only = any(
        phrase in text
        for phrase in [
            "singapore citizen or permanent resident",
            "singapore citizens and permanent residents",
            "singaporean or pr",
            "citizens/pr only",
            "no work pass",
        ]
    )
    if citizen_pr_only:
        if is_unknown(profile, "work_status"):
            reasons.append("Listing appears restricted to Singapore citizens or permanent residents.")
        elif work_status not in {"SINGAPORE CITIZEN", "CITIZEN", "SINGAPORE PR", "PR", "PERMANENT RESIDENT"}:
            blockers.append("Listing appears restricted to Singapore citizens or permanent residents.")

    no_sponsorship = any(
        phrase in text
        for phrase in [
            "no sponsorship",
            "unable to sponsor",
            "must not require sponsorship",
            "without sponsorship",
            "existing right to work",
        ]
    )
    if no_sponsorship:
        if sponsorship in {"YES", "TRUE", "REQUIRED"}:
            blockers.append("Employer states that sponsorship is unavailable.")
        elif work_status not in NO_PASS_WORK_STATUSES and is_unknown(profile, "sponsorship_required"):
            reasons.append("Employer may not provide sponsorship; user confirmation is required.")

    student_required = "[uni" in title or any(
        phrase in text
        for phrase in [
            "currently enrolled",
            "current student",
            "returning to university",
            "penultimate year",
            "must be a student",
            "full-time undergraduate student",
            "full time undergraduate student",
            "full-time undergraduate or master",
            "full time undergraduate or master",
            "pursuing a bachelor",
            "pursuing an undergraduate",
            "university student",
        ]
    ) or bool(
        re.search(
            r"\b(?:(?:ideal|eligible) candidates?\s+)?(?:must|should|required to|currently)\s+be\s+pursuing\b|"
            r"\bstudents?\s+(?:currently\s+)?pursuing\b|"
            r"\bpursuing\s+(?:an?\s+)?(?:degree|diploma)\s+in\b",
            text,
        )
    )
    if internship_role and re.search(
        r"\b(?:enrolled|enrollment)\s+in\s+(?:an?\s+)?(?:bachelor|master|undergraduate|university)\b|"
        r"\bundergraduate\s+in\s+(?:computer science|engineering|artificial intelligence|machine learning|"
        r"data science|robotics|a related field)\b",
        text,
    ):
        student_required = True
    current_student = field_value(profile, "current_student")
    graduation_date = field_value(profile, "graduation_date")
    try:
        graduation_has_passed = bool(graduation_date) and date.fromisoformat(graduation_date) < date.today()
    except ValueError:
        graduation_has_passed = False
    if graduation_has_passed and current_student in {"YES", "TRUE"}:
        current_student = "NO"
    if student_required:
        if graduation_has_passed:
            reasons.append(
                "Confirmed graduation date has passed, so the older current-student answer is no longer used for this student-only vacancy."
            )
        if current_student in {"NO", "FALSE"}:
            blockers.append("Listing requires current student status.")
        elif is_unknown(profile, "current_student"):
            reasons.append("Listing requires current student status; confirmation is missing.")

    if blockers:
        status = "INELIGIBLE"
        score = 0
    elif unknown_fields:
        status = "ELIGIBILITY_UNKNOWN"
        score = 50
        reasons.append("Missing confirmed fields: " + ", ".join(sorted(set(unknown_fields))))
    else:
        status = "ELIGIBLE"
        score = 100
        reasons.append("No eligibility conflict was detected from confirmed profile fields.")

    return {
        "eligibility_status": status,
        "eligibility_score": score,
        "eligibility_reasons": list(dict.fromkeys(reasons + blockers)),
        "eligibility_unknown_fields": sorted(set(unknown_fields)),
        "eligibility_blockers": blockers,
    }


def eligibility_profile_report(profile: dict[str, Any] | None = None) -> str:
    profile = profile if profile is not None else load_eligibility_profile()
    labels = [
        "work_status",
        "sponsorship_required",
        "graduation_date",
        "current_student",
        "internship_eligible",
        "notice_period_days",
        "available_from",
        "minimum_salary_intern_sgd",
        "minimum_salary_full_time_sgd",
    ]
    if field_value(profile, "work_status") not in NO_PASS_WORK_STATUSES:
        labels[1:1] = ["pass_type", "pass_expiry"]
    lines = ["# Eligibility Profile", ""]
    for key in labels:
        value = field_value(profile, key) or "UNKNOWN"
        confirmation = field_confirmation(profile, key) or "UNCONFIRMED"
        lines.append(f"- {key}: {value} ({confirmation})")
    lines.extend(
        [
            "",
            "Sensitive-data rule: never store NRIC, passport numbers, Singpass credentials, bank details, or tax identifiers here.",
        ]
    )
    return "\n".join(lines)
