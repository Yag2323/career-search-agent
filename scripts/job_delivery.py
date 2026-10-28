from __future__ import annotations

import argparse
from typing import Any, Iterable

from career_agent_common import clean
from career_store import CareerStore
from job_identity import canonical_key, canonicalize_url


def delivery_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        clean(row.get("job_id")).upper(),
        canonical_key(row),
        canonicalize_url(clean(row.get("canonical_url") or row.get("link"))),
    )


def was_delivered(row: dict[str, Any], store: CareerStore | None = None) -> bool:
    job_id, key, url = delivery_identity(row)
    if not key:
        return False
    return (store or CareerStore()).was_job_delivered(job_id, key, url)


def filter_undelivered(
    rows: Iterable[dict[str, Any]],
    store: CareerStore | None = None,
) -> list[dict[str, Any]]:
    target = store or CareerStore()
    return [dict(row) for row in rows if not was_delivered(row, target)]


def record_deliveries(
    rows: Iterable[dict[str, Any]],
    *,
    channel: str,
    message_ids: list[int] | None = None,
    store: CareerStore | None = None,
) -> int:
    target = store or CareerStore()
    created = 0
    for row in rows:
        job_id, key, url = delivery_identity(row)
        if not key:
            continue
        created += int(
            target.record_job_delivery(
                job_id=job_id,
                delivery_key=key,
                canonical_url=url,
                channel=channel,
                message_ids=message_ids,
                data={
                    "company": clean(row.get("company")),
                    "title": clean(row.get("title")),
                    "location": clean(row.get("location")),
                    "link": clean(row.get("link")),
                },
            )
        )
    return created


def claim_deliveries(
    rows: Iterable[dict[str, Any]],
    *,
    channel: str,
    store: CareerStore | None = None,
) -> list[dict[str, Any]]:
    target = store or CareerStore()
    claimed: list[dict[str, Any]] = []
    for row in rows:
        job_id, key, url = delivery_identity(row)
        if not key:
            continue
        if target.claim_job_delivery(
            job_id=job_id,
            delivery_key=key,
            canonical_url=url,
            channel=channel,
            data={
                "company": clean(row.get("company")),
                "title": clean(row.get("title")),
                "location": clean(row.get("location")),
                "link": clean(row.get("link")),
            },
        ):
            claimed.append(dict(row))
    return claimed


def finalize_deliveries(
    rows: Iterable[dict[str, Any]],
    *,
    channel: str,
    message_ids: list[int] | None = None,
    store: CareerStore | None = None,
) -> None:
    target = store or CareerStore()
    for row in rows:
        _job_id, key, _url = delivery_identity(row)
        if key:
            target.finalize_job_delivery(key, channel, message_ids)


def release_deliveries(rows: Iterable[dict[str, Any]], store: CareerStore | None = None) -> None:
    target = store or CareerStore()
    for row in rows:
        _job_id, key, _url = delivery_identity(row)
        if key:
            target.release_job_delivery(key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or seed the permanent Telegram job-delivery ledger.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    seed = subparsers.add_parser("seed")
    seed.add_argument("job_ids", nargs="+")
    args = parser.parse_args()

    store = CareerStore()
    if args.command == "status":
        deliveries = store.list_job_deliveries()
        print(f"Delivered jobs: {len(deliveries)}")
        for item in deliveries[:20]:
            print(f"{item['job_id']} | {item['first_delivered_at']} | {item['delivery_channel']}")
        return

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw_id in args.job_ids:
        row = store.get_job(raw_id)
        if row:
            rows.append(row)
        else:
            missing.append(raw_id.upper())
    created = record_deliveries(rows, channel="HISTORICAL_SEED", store=store)
    print(f"Seeded deliveries: {created}")
    if missing:
        print("Unknown job IDs: " + ", ".join(missing))


if __name__ == "__main__":
    main()
