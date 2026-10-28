from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from career_agent_common import APPLICATIONS_DIR, PROFILE_DIR, clean, find_job, safe_job_id, write_text

EVIDENCE_PATH = PROFILE_DIR / "evidence_catalog.json"
EVIDENCE_EXAMPLE_PATH = PROFILE_DIR / "evidence_catalog.example.json"

EXTERNAL_SKILLS = [
    "pytorch",
    "tensorflow",
    "scikit-learn",
    "numpy",
    "pandas",
    "opencv",
    "ros",
    "ros2",
    "cuda",
    "tensorrt",
    "aws",
    "azure",
    "gcp",
    "kubernetes",
    "docker",
    "python",
    "c++",
    "c#",
    "java",
    "javascript",
    "matlab",
    "sql",
    "git",
    "linux",
    "machine learning",
    "deep learning",
    "computer vision",
    "robotics",
    "embedded systems",
    "firmware",
    "iot",
    "stm32",
    "raspberry pi",
    "jetson nano",
    "serial communication",
    "data preparation",
    "model evaluation",
    "testing",
    "debugging",
    "technical documentation",
]

MANDATORY_MARKERS = [
    "must have",
    "must be",
    "required",
    "requirement",
    "minimum",
    "proficient",
    "strong knowledge",
    "experience with",
    "experience in",
]

PREFERRED_MARKERS = [
    "preferred",
    "nice to have",
    "advantageous",
    "bonus",
    "plus",
    "ideally",
]

RESPONSIBILITY_MARKERS = [
    "develop",
    "design",
    "implement",
    "build",
    "test",
    "debug",
    "evaluate",
    "analyse",
    "analyze",
    "document",
    "collaborate",
    "support",
    "maintain",
]


def load_catalog(path: Path = EVIDENCE_PATH) -> dict[str, Any]:
    target = path
    if not target.exists() and target == EVIDENCE_PATH and EVIDENCE_EXAMPLE_PATH.exists():
        target = EVIDENCE_EXAMPLE_PATH
    if not target.exists():
        return {"schema_version": 1, "claims": [], "known_missing_skills": []}
    return json.loads(target.read_text(encoding="utf-8"))


def job_text(row: dict[str, Any]) -> str:
    return "\n".join(
        clean(row.get(key))
        for key in ["title", "snippet", "extracted_content"]
        if clean(row.get(key))
    )


def fragments(text: str) -> list[str]:
    raw_parts = re.split(r"(?:\r?\n|[•●▪]|(?<=[.!?])\s+|\s+[|]\s+)", text)
    output: list[str] = []
    for raw in raw_parts:
        item = clean(re.sub(r"^[\-*#\d.)\s]+", "", raw))
        if 12 <= len(item) <= 500:
            output.append(item)
    return list(dict.fromkeys(output))


def context_for(text: str, term: str, index: int | None = None) -> str:
    lower = text.lower()
    index = lower.find(term.lower()) if index is None else index
    if index < 0:
        return term
    start = max(
        lower.rfind(".", 0, index),
        lower.rfind("\n", 0, index),
        lower.rfind(" * ", 0, index),
        lower.rfind(" ### ", 0, index),
    ) + 1
    end_candidates = [
        position
        for position in [
            lower.find(".", index),
            lower.find("\n", index),
            lower.find(" * ", index + len(term)),
            lower.find(" ### ", index + len(term)),
        ]
        if position >= 0
    ]
    end = min(end_candidates) + 1 if end_candidates else min(len(text), index + 300)
    return clean(text[start:end])[:300] or term


def evidence_index(catalog: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for claim in catalog.get("claims", []):
        for term in claim.get("terms", []):
            index.setdefault(clean(term).lower(), []).append(claim)
    return index


def requirement_level(context: str) -> str:
    lower = context.lower()
    if any(marker in lower for marker in PREFERRED_MARKERS):
        return "PREFERRED"
    if any(marker in lower for marker in MANDATORY_MARKERS):
        return "MANDATORY"
    return "MENTIONED"


def level_at_position(text: str, index: int, context: str) -> str:
    lower = text.lower()
    required_position = max(lower.rfind("required skills", 0, index), lower.rfind("requirements", 0, index))
    preferred_position = max(lower.rfind("preferred skills", 0, index), lower.rfind("preferred qualifications", 0, index))
    if preferred_position > required_position:
        return "PREFERRED"
    if required_position > preferred_position:
        return "MANDATORY"
    return requirement_level(context)


def best_occurrence(text: str, term: str) -> tuple[str, str]:
    priorities = {"MANDATORY": 3, "PREFERRED": 2, "MENTIONED": 1}
    best_level = "MENTIONED"
    best_context = term
    pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        context = context_for(text, term, match.start())
        level = level_at_position(text, match.start(), context)
        if best_context == term or priorities[level] > priorities[best_level]:
            best_level = level
            best_context = context
    return best_level, best_context


def requirement_status(term: str, level: str, evidence: dict[str, list[dict[str, Any]]]) -> tuple[str, list[str]]:
    claims = evidence.get(term.lower(), [])
    verified = [claim for claim in claims if clean(claim.get("status")).upper() == "VERIFIED"]
    unconfirmed = [claim for claim in claims if clean(claim.get("status")).upper() != "VERIFIED"]
    if verified:
        return "MATCHED", [clean(claim.get("evidence")) for claim in verified]
    if unconfirmed:
        return "UNKNOWN", [clean(claim.get("evidence")) for claim in unconfirmed]
    if level in {"MANDATORY", "PREFERRED"}:
        return "MISSING", []
    return "UNKNOWN", []


def extract_years(text: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for match in re.finditer(
        r"(?P<count>\d+)\s*(?:\+|or more|or greater)?\s*(?:years?|yrs?)\b(?=[^.:;\n]{0,90}\bexperience\b)",
        text,
        flags=re.IGNORECASE,
    ):
        count = int(match.group("count"))
        context = context_for(text, match.group(0))
        output.append(
            {
                "term": f"{count}+ years experience",
                "level": "MANDATORY",
                "status": "MISSING" if count > 0 else "MATCHED",
                "context": context,
                "evidence": [],
            }
        )
    return output


def extract_degree_requirement(text: str, catalog_index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    lower = text.lower()
    advanced_degree = re.search(
        r"(?:master['’]?s|masters|ph\.?d|doctoral).{0,100}(?:degree|candidate)|"
        r"(?:degree|candidate).{0,100}(?:master['’]?s|masters|ph\.?d|doctoral)",
        lower,
    )
    advanced_context = context_for(text, advanced_degree.group(0), advanced_degree.start()) if advanced_degree else ""
    bachelor_alternative = bool(
        re.search(r"\b(?:bs|b\.s\.?|bsc|bachelor['’]?s?)\s*(?:/|,|or)", advanced_context, flags=re.IGNORECASE)
        or re.search(r"\b(?:bachelor['’]?s?|undergraduate).{0,80}\b(?:master['’]?s?|ph\.?d)\b", advanced_context, flags=re.IGNORECASE)
    )
    if advanced_degree and not bachelor_alternative:
        return [
            {
                "term": "Master's or PhD degree",
                "level": "MANDATORY",
                "status": "MISSING",
                "context": context_for(text, advanced_degree.group(0), advanced_degree.start()),
                "evidence": [],
            }
        ]
    if not any(term in lower for term in ["bachelor", "degree in", "b.eng", "engineering degree"]) and not re.search(
        r"\b(?:bs|b\.s\.?|bsc)\s*(?:/|,|or)", lower
    ):
        return []
    context_term = "bachelor" if "bachelor" in lower else ("degree in" if "degree in" in lower else "bs")
    context = context_for(text, context_term)
    claims = catalog_index.get("bachelor degree", []) or catalog_index.get("engineering degree", [])
    evidence = [clean(claim.get("evidence")) for claim in claims if clean(claim.get("status")).upper() == "VERIFIED"]
    return [
        {
            "term": "relevant bachelor degree",
            "level": requirement_level(context),
            "status": "MATCHED" if evidence else "UNKNOWN",
            "context": context,
            "evidence": evidence,
        }
    ]


def extract_authorization(text: str) -> list[str]:
    output: list[str] = []
    for phrase in [
        "singapore citizen",
        "permanent resident",
        "work authorization",
        "right to work",
        "sponsorship",
        "work pass",
        "currently enrolled",
        "current student",
    ]:
        if phrase in text.lower():
            output.append(context_for(text, phrase))
    return list(dict.fromkeys(output))


def match_requirements(row: dict[str, Any], catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog = catalog or load_catalog()
    evidence = evidence_index(catalog)
    text = job_text(row)
    lower = text.lower()
    all_terms = sorted(set(EXTERNAL_SKILLS) | set(evidence.keys()), key=len, reverse=True)
    requirements: list[dict[str, Any]] = []

    for term in all_terms:
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        if not re.search(pattern, lower, flags=re.IGNORECASE):
            continue
        level, context = best_occurrence(text, term)
        status, sources = requirement_status(term, level, evidence)
        requirements.append(
            {
                "term": term,
                "level": level,
                "status": status,
                "context": context,
                "evidence": list(dict.fromkeys(sources)),
            }
        )

    requirements.extend(extract_years(text))
    requirements.extend(extract_degree_requirement(text, evidence))

    deduped: dict[str, dict[str, Any]] = {}
    priority = {"MANDATORY": 3, "PREFERRED": 2, "MENTIONED": 1}
    for item in requirements:
        key = clean(item.get("term")).lower()
        existing = deduped.get(key)
        if not existing or priority.get(item["level"], 0) > priority.get(existing["level"], 0):
            deduped[key] = item
    requirements = sorted(deduped.values(), key=lambda item: (-priority.get(item["level"], 0), item["term"]))

    job_fragments = fragments(text)
    responsibilities = [
        item
        for item in job_fragments
        if any(re.search(rf"\b{re.escape(marker)}\w*\b", item.lower()) for marker in RESPONSIBILITY_MARKERS)
        and not any(marker in item.lower() for marker in MANDATORY_MARKERS + PREFERRED_MARKERS)
    ][:8]

    mandatory = [item for item in requirements if item["level"] == "MANDATORY"]
    preferred = [item for item in requirements if item["level"] == "PREFERRED"]
    matched = [item for item in requirements if item["status"] == "MATCHED"]
    missing = [item for item in requirements if item["status"] == "MISSING"]
    unknown = [item for item in requirements if item["status"] == "UNKNOWN"]
    total = len(requirements)
    evidence_coverage = round(100 * len(matched) / total) if total else 0
    extraction_confidence = min(95, 35 + min(len(text) // 250, 25) + min(total * 4, 35)) if text else 0
    confidence = round((evidence_coverage * 0.65) + (extraction_confidence * 0.35)) if total else 20

    return {
        "requirements": requirements,
        "mandatory_requirements": [item["term"] for item in mandatory],
        "preferred_requirements": [item["term"] for item in preferred],
        "responsibilities": responsibilities,
        "matched_requirements": [item["term"] for item in matched],
        "missing_requirements": [item["term"] for item in missing],
        "unknown_requirements": [item["term"] for item in unknown],
        "work_authorization_restrictions": extract_authorization(text),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "unknown_count": len(unknown),
        "requirement_count": total,
        "match_confidence": confidence,
        "extraction_confidence": extraction_confidence,
    }


def enrich_job_with_requirements(row: dict[str, Any]) -> dict[str, str]:
    result = match_requirements(row)
    return {
        **{str(key): clean(value) for key, value in row.items()},
        "mandatory_requirements": "; ".join(result["mandatory_requirements"]),
        "preferred_requirements": "; ".join(result["preferred_requirements"]),
        "responsibilities": "; ".join(result["responsibilities"]),
        "matched_requirements": "; ".join(result["matched_requirements"]),
        "missing_requirements": "; ".join(result["missing_requirements"]),
        "unknown_requirements": "; ".join(result["unknown_requirements"]),
        "work_authorization_restrictions": "; ".join(result["work_authorization_restrictions"]),
        "matched_count": str(result["matched_count"]),
        "missing_count": str(result["missing_count"]),
        "unknown_count": str(result["unknown_count"]),
        "requirement_count": str(result["requirement_count"]),
        "match_confidence": str(result["match_confidence"]),
        "extraction_confidence": str(result["extraction_confidence"]),
        "requirements_json": json.dumps(result["requirements"], ensure_ascii=False),
    }


def report_markdown(job: dict[str, Any], result: dict[str, Any]) -> str:
    lines = [
        f"# Requirement Match Report - {clean(job.get('job_id'))}",
        "",
        f"- Company: {clean(job.get('company'))}",
        f"- Role: {clean(job.get('title'))}",
        f"- Result: {result['matched_count']} matched, {result['missing_count']} missing, {result['unknown_count']} unknown",
        f"- Match confidence: {result['match_confidence']}%",
        f"- Extraction confidence: {result['extraction_confidence']}%",
        "",
        "## Evidence Matrix",
        "",
        "| Requirement | Level | Result | Evidence |",
        "|---|---|---|---|",
    ]
    for item in result["requirements"]:
        evidence = "; ".join(item["evidence"]) or "No verified evidence"
        lines.append(
            f"| {item['term'].replace('|', '/')} | {item['level']} | {item['status']} | {evidence.replace('|', '/')} |"
        )
    if not result["requirements"]:
        lines.append("| Job description did not expose structured requirements | UNKNOWN | UNKNOWN | Manual review required |")
    lines.extend(["", "## Responsibilities", ""])
    lines.extend(f"- {item}" for item in result["responsibilities"])
    if not result["responsibilities"]:
        lines.append("- No clear responsibilities were extracted; inspect the employer page manually.")
    lines.extend(["", "## Work-Authorization Signals", ""])
    lines.extend(f"- {item}" for item in result["work_authorization_restrictions"])
    if not result["work_authorization_restrictions"]:
        lines.append("- No explicit work-authorization wording was found.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract job requirements and match them to verified resume evidence.")
    parser.add_argument("job_id")
    args = parser.parse_args()
    job_id = safe_job_id(args.job_id)
    job = find_job(job_id)
    result = match_requirements(job)
    path = APPLICATIONS_DIR / f"{job_id}_match_report.md"
    write_text(path, report_markdown(job, result))
    print(f"Requirement report: {path}")
    print(
        f"Result: {result['matched_count']} matched, {result['missing_count']} missing, "
        f"{result['unknown_count']} unknown, confidence {result['match_confidence']}%"
    )


if __name__ == "__main__":
    main()
