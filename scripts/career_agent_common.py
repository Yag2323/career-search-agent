from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BASE / "scripts"
JOBS_DIR = BASE / "job_listings"
PROFILE_DIR = BASE / "profile"
RESUMES_DIR = BASE / "resumes"
RESUME_PDFS_DIR = RESUMES_DIR / "pdf"
COVER_LETTERS_DIR = BASE / "cover_letters"
APPLICATIONS_DIR = BASE / "applications"
PROMPTS_DIR = BASE / "prompts"
LOGS_DIR = BASE / "logs"
PDF_PREVIEWS_DIR = LOGS_DIR / "pdf_previews"
STATE_DIR = BASE / "state"
ENV_FILE = BASE / ".env"

EXPECTED_PHONE = os.environ.get("CANDIDATE_PHONE", "NOT_CONFIGURED").strip()
EXPECTED_EMAIL = os.environ.get("CANDIDATE_EMAIL", "NOT_CONFIGURED").strip()
EXPECTED_LINKEDIN = os.environ.get("CANDIDATE_LINKEDIN", "NOT_CONFIGURED").strip()
EXPECTED_GITHUB = os.environ.get("CANDIDATE_GITHUB", "NOT_CONFIGURED").strip()

for directory in [
    JOBS_DIR,
    PROFILE_DIR,
    RESUMES_DIR,
    RESUME_PDFS_DIR,
    COVER_LETTERS_DIR,
    APPLICATIONS_DIR,
    PROMPTS_DIR,
    LOGS_DIR,
    PDF_PREVIEWS_DIR,
    STATE_DIR,
]:
    directory.mkdir(parents=True, exist_ok=True)


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_job_id(job_id: str) -> str:
    normalized = clean(job_id).upper()
    if not re.fullmatch(r"JOB-(?:\d{3,6}|[A-F0-9]{8,12})", normalized):
        raise ValueError(f"Invalid job id: {job_id}")
    try:
        from career_store import CareerStore

        return CareerStore().resolve_job_id(normalized) or normalized
    except Exception:
        return normalized


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def merged_env() -> dict[str, str]:
    env = read_env_file()
    try:
        from secure_config import load_secure_values

        env.update(load_secure_values())
    except Exception:
        pass
    env.update(os.environ)
    return env


def require_env(name: str, env: dict[str, str] | None = None) -> str:
    source = env or merged_env()
    value = source.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} in secure storage, the process environment, or {ENV_FILE}")
    return value


def telegram_config(env: dict[str, str] | None = None) -> tuple[str, str]:
    source = env or merged_env()
    return require_env("TELEGRAM_BOT_TOKEN", source), require_env("TELEGRAM_CHAT_ID", source)


def send_telegram_message(
    message: str,
    env: dict[str, str] | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> list[int]:
    import requests

    token, chat_id = telegram_config(env)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = split_message(message)
    message_ids: list[int] = []
    for index, chunk in enumerate(chunks):
        data: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if reply_markup and index == len(chunks) - 1:
            data["reply_markup"] = json.dumps(reply_markup)
        response = requests.post(
            url,
            data=data,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        if result.get("message_id") is not None:
            message_ids.append(int(result["message_id"]))
    return message_ids


def send_telegram_file(
    file_path: Path | str,
    caption: str = "",
    env: dict[str, str] | None = None,
) -> int | None:
    import requests

    token, chat_id = telegram_config(env)
    path = Path(file_path)
    if not path.is_absolute():
        path = BASE / path
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with path.open("rb") as handle:
        response = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"document": handle},
            timeout=60,
        )
    response.raise_for_status()
    result = response.json().get("result", {})
    message_id = result.get("message_id")
    return int(message_id) if message_id is not None else None


def split_message(message: str, limit: int = 3800) -> list[str]:
    text = str(message)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < 500:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def get_telegram_updates(offset: int | None, timeout: int = 30, env: dict[str, str] | None = None) -> dict[str, Any]:
    import requests

    token, _chat_id = telegram_config(env)
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    response = requests.get(url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    return response.json()


def answer_telegram_callback(callback_query_id: str, text: str = "", env: dict[str, str] | None = None) -> None:
    import requests

    token, _chat_id = telegram_config(env)
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text
    response = requests.post(url, data=data, timeout=20)
    response.raise_for_status()


def run_logged(
    command: list[str],
    label: str,
    log_path: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = env or merged_env()

    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n\n" + "=" * 80 + "\n")
        log.write(f"{now_stamp()} - {label}\n")
        log.write("Command: " + redact_text(" ".join(command), child_env) + "\n")
        log.write("=" * 80 + "\n")
        log.flush()

        result = subprocess.run(
            command,
            cwd=BASE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout,
        )
        log.write(redact_text(result.stdout or "", child_env))

    if result.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}. See {log_path}")
    return result


def tail_file(path: Path, lines: int = 80) -> str:
    try:
        value = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        return redact_text(value)
    except Exception:
        return ""


def redact_text(value: str, env: dict[str, str] | None = None) -> str:
    text = str(value or "")
    source = env or merged_env()
    for key, secret in source.items():
        upper = key.upper()
        if not secret or not any(marker in upper for marker in ["TOKEN", "KEY", "SECRET", "PASSWORD", "CHAT_ID"]):
            continue
        if len(secret) >= 4:
            text = text.replace(secret, f"[REDACTED_{upper}]")
    text = re.sub(r"[\w.\-+]+@[\w.\-]+\.\w+", "[REDACTED_EMAIL]", text)
    text = re.sub(r"\+?\d[\d\s-]{7,}\d", "[REDACTED_PHONE]", text)
    text = re.sub(r"\b[STFG]\d{7}[A-Z]\b", "[REDACTED_NRIC]", text, flags=re.IGNORECASE)
    return text


def load_jobs(csv_path: Path | None = None) -> list[dict[str, str]]:
    path = csv_path or (JOBS_DIR / "latest_jobs.csv")
    if not path.exists():
        raise FileNotFoundError(f"Missing job CSV: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_job(job_id: str, rows: list[dict[str, str]] | None = None) -> dict[str, str]:
    target = safe_job_id(job_id)
    source = rows if rows is not None else load_jobs()
    for row in source:
        if clean(row.get("job_id")).upper() == target:
            return row
    try:
        from career_store import CareerStore

        stored = CareerStore().get_job(target)
        if stored:
            return {str(key): clean(value) for key, value in stored.items()}
    except Exception:
        pass
    raise KeyError(f"{target} was not found in {JOBS_DIR / 'latest_jobs.csv'}")


def domain_of(url: str) -> str:
    try:
        return urlparse(clean(url)).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def job_summary(row: dict[str, str]) -> str:
    return "\n".join(
        [
            f"Job ID: {clean(row.get('job_id'))}",
            f"Company: {clean(row.get('company'))}",
            f"Role: {clean(row.get('title'))}",
            f"Location: {clean(row.get('location'))}",
            f"Remote type: {clean(row.get('remote_type'))}",
            f"Quality score: {clean(row.get('quality_score')) or clean(row.get('match_score'))}",
            f"Apply type: {clean(row.get('apply_type')) or clean(row.get('review_type'))}",
            f"Eligibility: {clean(row.get('eligibility_status')) or 'ELIGIBILITY_UNKNOWN'}",
            (
                "Requirement match: "
                f"{clean(row.get('matched_count')) or '0'} matched, "
                f"{clean(row.get('missing_count')) or '0'} missing, "
                f"{clean(row.get('unknown_count')) or '0'} unknown "
                f"({clean(row.get('match_confidence')) or '0'}% confidence)"
            ),
            f"Salary/Stipend: {clean(row.get('salary_text')) or 'Not listed'}",
            f"Application deadline: {clean(row.get('application_deadline')) or 'Not listed'}",
            f"Portfolio highlight: {clean(row.get('portfolio_project')) or 'STM32 + UWP Building Monitor'}",
            f"Risk flags: {clean(row.get('risk_flags')) or 'None'}",
            f"Source count: {clean(row.get('source_count')) or '1'}",
            f"Link: {clean(row.get('link'))}",
        ]
    )


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def relative_to_base(path: Path | str) -> str:
    p = Path(path)
    try:
        return str(p.relative_to(BASE))
    except ValueError:
        return str(p)


def append_application_log(
    row: dict[str, str],
    status: str,
    resume_file: Path | str = "",
    cover_letter_file: Path | str = "",
    notes: str = "",
    date_applied: str = "",
) -> None:
    log_path = APPLICATIONS_DIR / "application_log.csv"
    fieldnames = [
        "job_id",
        "date_found",
        "company",
        "title",
        "location",
        "remote_type",
        "link",
        "match_score",
        "status",
        "resume_file",
        "cover_letter_file",
        "notes",
        "date_applied",
    ]

    exists = log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists or log_path.stat().st_size == 0:
            writer.writeheader()
        writer.writerow(
            {
                "job_id": clean(row.get("job_id")),
                "date_found": clean(row.get("date_found")),
                "company": clean(row.get("company")),
                "title": clean(row.get("title")),
                "location": clean(row.get("location")),
                "remote_type": clean(row.get("remote_type")),
                "link": clean(row.get("link")),
                "match_score": clean(row.get("match_score")),
                "status": status,
                "resume_file": relative_to_base(resume_file) if resume_file else "",
                "cover_letter_file": relative_to_base(cover_letter_file) if cover_letter_file else "",
                "notes": notes,
                "date_applied": date_applied,
            }
        )


def missing_profile_answers(profile_text: str) -> list[str]:
    checks = [
        "Availability",
        "Expected salary",
        "Work authorization answer",
        "Sponsorship answer",
        "Relocation answer",
        "Why are you interested in this company",
    ]
    missing: list[str] = []
    lines = profile_text.splitlines()
    for label in checks:
        found = False
        has_answer = False
        pattern = re.compile(rf"^{re.escape(label)}:\s*(?P<inline>.*)$", re.IGNORECASE)
        for index, line in enumerate(lines):
            match = pattern.match(line.strip())
            if not match:
                continue
            found = True
            inline = clean(match.group("inline"))
            if inline:
                has_answer = True
                break
            for next_line in lines[index + 1 :]:
                candidate = next_line.strip()
                if not candidate:
                    continue
                if candidate.startswith("## ") or re.match(r"^[A-Za-z][A-Za-z /]+:\s*$", candidate):
                    break
                has_answer = True
                break
            break
        if not found or not has_answer:
            missing.append(label)
    return missing


def contact_conflict_warnings(profile_text: str, resume_text: str) -> list[str]:
    warnings: list[str] = []

    if EXPECTED_EMAIL.lower() not in resume_text.lower():
        warnings.append(f"Master resume is missing correct email: {EXPECTED_EMAIL}")
    if EXPECTED_PHONE not in resume_text:
        warnings.append(f"Master resume is missing correct phone: {EXPECTED_PHONE}")
    if EXPECTED_LINKEDIN.lower() not in resume_text.lower():
        warnings.append(f"Master resume is missing correct LinkedIn: {EXPECTED_LINKEDIN}")
    if EXPECTED_GITHUB.lower() not in resume_text.lower():
        warnings.append(f"Master resume is missing correct GitHub: {EXPECTED_GITHUB}")

    email_pattern = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")
    profile_emails = {item.lower() for item in email_pattern.findall(profile_text)}
    resume_emails = {item.lower() for item in email_pattern.findall(resume_text)}
    if profile_emails and resume_emails and profile_emails != resume_emails:
        warnings.append("Profile email and master resume email differ.")

    phone_pattern = re.compile(r"\+?\d[\d\s-]{7,}\d")
    profile_phones = {clean(item) for item in phone_pattern.findall(profile_text)}
    resume_phones = {clean(item) for item in phone_pattern.findall(resume_text)}
    if profile_phones and resume_phones and profile_phones != resume_phones:
        warnings.append("Profile phone and master resume phone differ.")

    return warnings


def job_text(row: dict[str, str]) -> str:
    return " ".join(
        [
            clean(row.get("company")),
            clean(row.get("title")),
            clean(row.get("snippet")),
            clean(row.get("extracted_content")),
            clean(row.get("source_query")),
        ]
    )


def select_resume_variant(row: dict[str, str]) -> tuple[str, Path, str]:
    text = job_text(row).lower()
    ai_rd_terms = [
        "ai research",
        "research & development",
        "research and development",
        "computer vision",
        "generative ai",
        " llm",
        "llms",
        "large language model",
        "pytorch",
        "tensorflow",
        "model evaluation",
        "data preparation",
        "benchmarking",
        "proof-of-concept",
        "proof of concept",
        "prototype",
        "prototyping",
    ]
    matches = [term for term in ai_rd_terms if term.strip() in text]
    ai_rd_path = PROFILE_DIR / "ai_rd_intern_resume.md"

    if len(matches) >= 3 and ai_rd_path.exists():
        reason = "AI R&D variant selected because the job strongly matches: " + ", ".join(matches[:6])
        return "AI_R_AND_D_INTERN", ai_rd_path, reason

    return (
        "AI_ROBOTICS_GRADUATE_CANDIDATE",
        PROFILE_DIR / "master_resume.md",
        "Default AI & Robotics Graduate Candidate resume selected.",
    )


def resume_pre_submit_warnings(resume_text: str, row: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    lower_resume = resume_text.lower()

    meta_terms = [
        "target role:",
        "target company:",
        "resume source",
        "variant used",
        "review warnings",
        "deterministic fallback",
        "hermes output",
        "tailoring this resume",
        "this resume is tailored",
        "no professional ai industry experience is claimed",
    ]
    for term in meta_terms:
        if term in lower_resume:
            warnings.append(f"Resume contains workflow/meta wording: {term}")

    if EXPECTED_PHONE not in resume_text:
        warnings.append(f"Resume is missing correct phone number: {EXPECTED_PHONE}")
    if EXPECTED_EMAIL.lower() not in lower_resume:
        warnings.append(f"Resume is missing correct email: {EXPECTED_EMAIL}")
    if EXPECTED_LINKEDIN.lower() not in lower_resume:
        warnings.append(f"Resume is missing correct LinkedIn: {EXPECTED_LINKEDIN}")
    if EXPECTED_GITHUB.lower() not in lower_resume:
        warnings.append(f"Resume is missing correct GitHub: {EXPECTED_GITHUB}")

    high_risk_terms = [
        "senior real time market data",
        "c++",
        " vp ",
        " avp ",
        "market data",
        "banking",
        "aws",
        "kubernetes",
        "react",
        "javascript",
        "ros",
        "cuda",
    ]
    allowed_source = read_text(PROFILE_DIR / "career_profile.md").lower() + "\n" + read_text(PROFILE_DIR / "master_resume.md").lower() + "\n" + read_text(PROFILE_DIR / "ai_rd_intern_resume.md").lower()
    for term in high_risk_terms:
        normalized = term.strip()
        if normalized in lower_resume and normalized not in allowed_source:
            warnings.append(f"Suspicious unsupported or exaggerated skill/claim found: {normalized}")

    work_experience_match = re.search(
        r"##\s*work experience(?P<section>.*?)(?:\n##\s+|\Z)",
        resume_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if work_experience_match:
        work_section = work_experience_match.group("section").lower()
        if "coursework" in work_section or "academic project" in work_section:
            warnings.append("Coursework or academic projects appear under Work Experience.")

    company = clean(row.get("company")).lower()
    title = clean(row.get("title")).lower()
    title_terms = {
        item.lower()
        for item in re.findall(r"[A-Za-z][A-Za-z+#/.-]{2,}", title)
        if len(item) > 3
    }
    title_terms -= {
        "singapore",
        "program",
        "apply",
        "before",
        "jobs",
        "career",
        "careers",
    }
    company_present = bool(company and company in lower_resume)
    title_match_count = sum(1 for term in title_terms if term in lower_resume)

    terms = [
        item.lower()
        for item in re.findall(r"[A-Za-z][A-Za-z+#/.-]{2,}", job_text(row))
        if len(item) > 3
    ]
    stop_words = {
        "singapore",
        "apply",
        "before",
        "career",
        "careers",
        "company",
        "description",
        "engineer",
        "intern",
        "internship",
        "program",
        "review",
        "software",
        "system",
        "work",
    }
    useful_terms = [term for term in terms if term not in stop_words]
    matched_terms = {term for term in useful_terms if term in lower_resume}
    if useful_terms and len(matched_terms) < 2 and not company_present and title_match_count < 2:
        warnings.append("Resume may not be sufficiently tailored to the job description.")

    return warnings
