# Architecture

## Pipeline

1. `job_search_singapore_tavily.py` queries the configured provider router across targeted Singapore role families.
2. `rapid_job_watch.py` reads public employer ATS feeds and learns additional supported boards.
3. `job_identity.py` canonicalizes links, generates stable IDs, and merges duplicate listings.
4. `job_snapshot.py` reopens each vacancy and records availability evidence and a content hash.
5. `career_targeting.py`, `eligibility.py`, `requirements_matcher.py`, and `job_quality.py` apply freshness, career-stage, eligibility, evidence, and relevance rules.
6. `career_store.py` persists stable identities, sources, snapshots, aliases, and delivery records in SQLite.
7. `job_delivery.py` atomically claims unseen jobs before notification and finalizes or releases each claim based on the Telegram result.
8. `daily_digest.py` and `telegram_career_agent.py` deliver the ranked, uncapped qualified set.

## Search Sources

Direct ATS sources are preferred because they provide the strongest employer and open-vacancy evidence. Web providers are used for discovery and automatic fallback:

1. Tavily
2. SerpApi
3. Jina Reader
4. Firecrawl
5. Apify

The router records provider failures without exposing credentials. The daily wrapper can continue with verified direct ATS results when broad discovery is unavailable.

## Identity And Deduplication

A vacancy receives a stable ID derived from normalized company, role, location, and canonical URL evidence. Alternate links are retained as sources and aliases. Before Telegram delivery, the SQLite ledger removes anything already delivered through a digest or rapid alert.

Delivery claims are atomic. If Telegram accepts the notification, the claim is finalized. If delivery fails, the claim is released so a later run can retry it.

## Runtime Directories

The application creates these local directories as needed:

| Directory | Contents |
|---|---|
| `job_listings/` | Current search outputs and vacancy snapshots |
| `state/` | SQLite database and watcher state |
| `logs/` | Bounded operational logs |
| `applications/` | Generated digest document |

All are excluded from version control.

## Safety Boundary

The Telegram agent is search-only. It can discover, verify, rank, deduplicate, and report vacancies. It cannot submit applications or handle login credentials, government identifiers, banking data, declarations, or CAPTCHAs.
