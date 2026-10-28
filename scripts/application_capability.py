from __future__ import annotations

from typing import Any

from career_agent_common import clean
from job_identity import domain_of, source_type

MANUAL_PORTAL_DOMAINS = {"mycareersfuture.gov.sg"}


def application_capability(row: dict[str, Any], apply_type: str = "") -> dict[str, str]:
    url = clean(row.get("availability_url") or row.get("canonical_url") or row.get("link"))
    site = domain_of(url) or "Unknown"
    availability = clean(row.get("availability_status")).upper() or "UNVERIFIED"
    detected_source = source_type(url) if url else "REVIEW"
    detected_apply_type = clean(apply_type or row.get("apply_type")).upper()

    base = {
        "application_site": site,
        "agent_can_submit": "NO",
    }

    if availability == "CLOSED":
        return {
            **base,
            "application_mode": "CLOSED",
            "agent_site_support": "BLOCKED",
            "agent_can_prepare": "NO",
            "application_guidance": "The vacancy is closed or expired and must not be used for an application.",
        }

    if availability != "OPEN":
        return {
            **base,
            "application_mode": "VERIFY_FIRST",
            "agent_site_support": "BLOCKED_PENDING_RECHECK",
            "agent_can_prepare": "NO",
            "application_guidance": "The live vacancy was not confirmed. Run RECHECK before tailoring or applying.",
        }

    if detected_apply_type == "SKIP":
        return {
            **base,
            "application_mode": "NOT_RECOMMENDED",
            "agent_site_support": "BLOCKED_BY_REVIEW",
            "agent_can_prepare": "NO",
            "application_guidance": "The vacancy is open, but eligibility, requirements, relevance, or safety checks recommend skipping it.",
        }

    if any(domain in site for domain in MANUAL_PORTAL_DOMAINS):
        return {
            **base,
            "application_mode": "MANUAL_PORTAL",
            "agent_site_support": "PREPARE_PACKET_ONLY",
            "agent_can_prepare": "YES",
            "application_guidance": (
                "The agent can prepare documents and open the portal. You must sign in, answer portal questions, "
                "review declarations, and submit it yourself."
            ),
        }

    if detected_apply_type == "AGGREGATOR_ONLY" or detected_source == "AGGREGATOR_OR_MANUAL":
        return {
            **base,
            "application_mode": "AGGREGATOR_DISCOVERY",
            "agent_site_support": "FIND_DIRECT_PAGE_FIRST",
            "agent_can_prepare": "NO",
            "application_guidance": (
                "This is a discovery listing. The agent must find and verify the employer's direct page before preparing an application."
            ),
        }

    if detected_apply_type == "MANUAL_REVIEW":
        return {
            **base,
            "application_mode": "MANUAL_REVIEW",
            "agent_site_support": "OPEN_ONLY",
            "agent_can_prepare": "NO",
            "application_guidance": "The site is live, but eligibility or its application flow needs manual review first.",
        }

    if detected_apply_type == "DIRECT_APPLY" or detected_source in {"DIRECT_APPLY", "COMPANY_CAREER_PAGE"}:
        return {
            **base,
            "application_mode": "DIRECT_SUPERVISED",
            "agent_site_support": "OPEN_AND_PREPARE",
            "agent_can_prepare": "YES",
            "application_guidance": (
                "The agent can open the verified employer or ATS form and prepare ordinary fields. You must handle login, "
                "CAPTCHA, sensitive or legal questions, declarations, and the final Submit button."
            ),
        }

    return {
        **base,
        "application_mode": "MANUAL_REVIEW",
        "agent_site_support": "OPEN_ONLY",
        "agent_can_prepare": "NO",
        "application_guidance": "The site is live, but its application flow needs manual review before any fields are prepared.",
    }


def enrich_application_capability(row: dict[str, Any], apply_type: str = "") -> dict[str, Any]:
    return {**row, **application_capability(row, apply_type)}
