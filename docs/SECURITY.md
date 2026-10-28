# Security And Privacy

## Secrets

Never commit `.env`, `.secrets/`, Telegram credentials, or search-provider keys. On Windows, `secure_config.py` stores supported values with DPAPI. In CI or non-Windows environments, inject secrets through environment variables.

## Candidate Data

The tracked files contain only anonymized examples. Keep real eligibility details and evidence in:

- `profile/eligibility_profile.json`
- `profile/evidence_catalog.json`

Both paths are ignored by Git. Resumes and generated application material are outside this project's active scope and are also ignored.

## Generated Data

Job pages and logs can contain recruiter names, job metadata, URLs, or search history. The following directories are ignored and should be retained only as long as needed:

- `state/`
- `logs/`
- `job_listings/`
- `applications/`

Use `retention.py` and `sanitize_logs.py` in recurring workflows.

## Before Publishing A Fork

Run a secret scan over the complete Git history and working tree. Check for email addresses, phone numbers, local user paths, API-key formats, resumes, databases, generated listings, and copied logs. Removing a secret from the latest commit does not remove it from earlier history; rotate any credential that was ever committed.

## Responsible Use

Respect website terms, robots policies, provider quotas, and local privacy law. Use conservative polling intervals and direct public APIs where available. This software does not bypass authentication, access controls, CAPTCHAs, or employer submission safeguards.
