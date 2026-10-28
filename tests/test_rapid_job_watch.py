from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from career_store import CareerStore  # noqa: E402
from job_snapshot import AvailabilityCheck, content_hash  # noqa: E402
from rapid_job_watch import (  # noqa: E402
    AtsFeedClient,
    FeedResult,
    RapidJobWatcher,
    board_from_url,
    discover_boards,
    is_target_job,
    workday_board_sources,
)


class FakeFeedClient:
    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = rounds
        self.calls = 0

    def fetch_board(self, source: dict[str, str], etag: str = "") -> FeedResult:
        index = min(self.calls, len(self.rounds) - 1)
        self.calls += 1
        return FeedResult(source["provider"], source["board"], self.rounds[index])


def open_check(row: dict, timeout: int) -> tuple[dict, AvailabilityCheck]:
    checked = dict(row)
    checked.update(
        {
            "availability_status": "OPEN",
            "availability_checked_at": "2026-07-15T10:00:00+00:00",
            "availability_http_status": "200",
            "availability_confidence": "HIGH",
            "availability_reason": "Application form is visible.",
            "availability_application_signal": "Apply now",
            "availability_url": row["link"],
            "availability_error": "",
        }
    )
    text = row.get("extracted_content") or row["title"]
    return checked, AvailabilityCheck(
        checked_at="2026-07-15T10:00:00+00:00",
        source_url=row["link"],
        http_status=200,
        availability_status="OPEN",
        availability_confidence="HIGH",
        reason="Application form is visible.",
        content_hash=content_hash(text),
        text=text,
        application_signal="Apply now",
    )


def good_score(row: dict) -> dict:
    return {
        **row,
        "quality_score": "86",
        "apply_type": "DIRECT_APPLY",
        "application_mode": "DIRECT_SUPERVISED",
        "agent_site_support": "OPEN_AND_PREPARE",
        "agent_can_prepare": "YES",
        "agent_can_submit": "NO",
        "portfolio_project": "Example Embedded Systems Project",
    }


class RapidJobWatchTests(unittest.TestCase):
    def test_board_discovery_supports_all_public_ats_types(self) -> None:
        rows = [
            {"link": "https://job-boards.greenhouse.io/embed/job_board?for=jumptrading", "company": ""},
            {"link": "https://jobs.lever.co/weride/abc", "company": "WeRide.ai"},
            {"link": "https://jobs.ashbyhq.com/palo-it?ashby_jid=abc", "company": "PALO IT"},
            {"link": "https://jobs.smartrecruiters.com/SikaAG/123-role", "company": "Sika"},
            {
                "link": "https://ntu.wd3.myworkdayjobs.com/en-US/Careers/job/Research-Engineer_R0001",
                "company": "ntu.wd3.myworkdayjobs.com",
            },
            {
                "link": "https://apply.workable.com/acme-robotics/j/ABC123",
                "company": "Acme Robotics",
            },
        ]

        boards = discover_boards(rows)

        self.assertEqual(
            {(row["provider"], row["board"]) for row in boards},
            {
                ("GREENHOUSE", "jumptrading"),
                ("LEVER", "weride"),
                ("ASHBY", "palo-it"),
                ("SMARTRECRUITERS", "SikaAG"),
                ("WORKABLE", "acme-robotics"),
                (
                    "WORKDAY",
                    "ntu.wd3.myworkdayjobs.com|Careers|/job/Research-Engineer_R0001",
                ),
            },
        )
        self.assertEqual(board_from_url("https://jobs.lever.co/weride/abc"), ("LEVER", "weride"))
        self.assertEqual(
            board_from_url(
                "https://ntu.wd3.myworkdayjobs.com/Careers/job/Singapore/Research-Engineer_R0001/apply/useMyLastApplication"
            ),
            (
                "WORKDAY",
                "ntu.wd3.myworkdayjobs.com|Careers|/job/Singapore/Research-Engineer_R0001",
            ),
        )
        self.assertEqual(boards[-1]["company"], "Nanyang Technological University (NTU)")

        full_boards = workday_board_sources(boards)
        self.assertEqual(
            full_boards,
            [
                {
                    "provider": "WORKDAY_BOARD",
                    "board": "ntu.wd3.myworkdayjobs.com|Careers",
                    "company": "Nanyang Technological University (NTU)",
                }
            ],
        )

    def test_workday_board_fetch_discovers_jobs_not_seeded_by_search(self) -> None:
        class FakeResponse:
            status_code = 200
            headers: dict[str, str] = {}

            def __init__(self, payload: dict) -> None:
                self.payload = payload

            def json(self) -> dict:
                return self.payload

        class FakeHttp:
            def __init__(self) -> None:
                self.posts: list[str] = []
                self.gets: list[str] = []

            def post(self, url: str, **_kwargs):
                self.posts.append(url)
                return FakeResponse(
                    {
                        "total": 2,
                        "jobPostings": [
                            {
                                "title": "Robotics Engineer (Fresh Graduate)",
                                "externalPath": "/job/Singapore/Robotics-Engineer_R100",
                                "locationsText": "Singapore",
                                "postedOn": "Posted Today",
                            },
                            {
                                "title": "Robotics Engineer",
                                "externalPath": "/job/London/Robotics-Engineer_R200",
                                "locationsText": "London",
                                "postedOn": "Posted Today",
                            },
                        ],
                    }
                )

            def get(self, url: str, **_kwargs):
                self.gets.append(url)
                return FakeResponse(
                    {
                        "jobPostingInfo": {
                            "title": "Robotics Engineer (Fresh Graduate)",
                            "jobDescription": "<p>Build mobile robots with Python and ROS.</p>",
                            "location": "Singapore",
                            "startDate": "2026-08-18",
                            "jobReqId": "R100",
                            "canApply": True,
                            "posted": True,
                            "externalUrl": "https://acme.wd1.myworkdayjobs.com/External/job/Singapore/Robotics-Engineer_R100",
                        }
                    }
                )

        http = FakeHttp()
        client = AtsFeedClient(session=http, workday_max_pages=2)
        result = client.fetch_board(
            {
                "provider": "WORKDAY_BOARD",
                "board": "acme.wd1.myworkdayjobs.com|External",
                "company": "Acme",
            }
        )

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0]["ats_source_id"], "R100")
        self.assertEqual(result.entries[0]["ats_board"], "acme.wd1.myworkdayjobs.com|External")
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(len(http.gets), 1)

    def test_workable_markdown_feed_maps_current_singapore_role(self) -> None:
        index = """# Acme - All Open Positions

| Title | Department | Location | Type | Salary | Posted | Details |
|-------|-----------|----------|------|--------|--------|---------|
| Embedded Systems Intern | Engineering | Singapore, Singapore | Full-time | SGD 1,500 | 2026-08-18 | [View](https://apply.workable.com/acme/jobs/view/ABC123.md) |
| Robotics Engineer | Engineering | London, UK | Full-time | - | 2026-08-18 | [View](https://apply.workable.com/acme/jobs/view/XYZ999.md) |
"""
        rows = AtsFeedClient._workable_index(index)
        detail = """# Embedded Systems Intern

> Acme Robotics · Singapore, Singapore · Full-time · Posted 2026-08-18

## Description

Build STM32 firmware and integrate sensors.

## Requirements

- Diploma or degree in embedded systems.

## Apply

[Apply at Acme](https://apply.workable.com/acme/j/ABC123/apply)
"""
        mapped = AtsFeedClient._workable_detail(
            detail,
            board="acme",
            fallback_company="Acme",
            metadata=rows[0],
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(mapped["company"], "Acme Robotics")
        self.assertEqual(mapped["source_published_at"], "2026-08-18T00:00:00Z")
        self.assertEqual(mapped["salary_text"], "SGD 1,500")
        self.assertIn("Diploma or degree", mapped["extracted_content"])

    def test_successfactors_rss_maps_and_deduplicates_official_jobs(self) -> None:
        feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
<title><![CDATA[Robotics Software Engineer (Jurong East, SG)]]></title>
<description><![CDATA[<p>Entry level candidates are welcome. Build ROS systems with C++.</p>]]></description>
<pubDate>Mon, 17 Aug 2026 16:00:00 GMT</pubDate>
<link>https://careers.example.sg/job/Robotics-Software-Engineer/12345/?utm_source=rss</link>
</item></channel></rss>"""

        class FakeResponse:
            status_code = 200
            text = feed

        class FakeHttp:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        result = AtsFeedClient(session=FakeHttp()).fetch_board(
            {
                "provider": "SUCCESSFACTORS_RSS",
                "board": "careers.example.sg|en_GB",
                "company": "Example Engineering",
            }
        )

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0]["title"], "Robotics Software Engineer")
        self.assertEqual(result.entries[0]["location"], "Jurong East, SG")
        self.assertEqual(result.entries[0]["source_published_at"], "2026-08-17T16:00:00Z")
        self.assertNotIn("utm_source", result.entries[0]["link"])

    def test_smartrecruiters_mid_senior_metadata_is_rejected(self) -> None:
        row = AtsFeedClient()._smartrecruiters(
            {
                "content": [
                    {
                        "id": "one",
                        "name": "AI Mission Engineer",
                        "company": {"name": "Acme"},
                        "location": {"city": "Singapore", "country": "SG"},
                        "experienceLevel": {"label": "Mid-Senior Level"},
                    }
                ]
            },
            "acme",
            "Acme",
        )[0]

        accepted, reason = is_target_job(row)

        self.assertFalse(accepted)
        self.assertIn("Mid-Senior Level", reason)

    def test_provider_payloads_map_publish_time_and_singapore_location(self) -> None:
        client = AtsFeedClient()
        greenhouse = client._greenhouse(
            {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Robotics Intern",
                        "location": {"name": "Singapore"},
                        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                        "content": "<p>Build robots</p>",
                        "updated_at": "2026-07-15T09:00:00Z",
                    }
                ]
            },
            "acme",
            "Acme",
        )[0]
        lever = client._lever(
            [
                {
                    "id": "two",
                    "text": "AI Intern",
                    "hostedUrl": "https://jobs.lever.co/acme/two",
                    "descriptionPlain": "Build ML models",
                    "createdAt": 1784106000000,
                    "categories": {"location": "Singapore", "commitment": "Intern"},
                }
            ],
            "acme",
            "Acme",
        )[0]
        ashby = client._ashby(
            {
                "jobs": [
                    {
                        "id": "three",
                        "title": "Embedded Intern",
                        "location": "Singapore",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/three",
                        "descriptionPlain": "Embedded systems",
                        "publishedAt": "2026-07-15T09:30:00Z",
                    }
                ]
            },
            "acme",
            "Acme",
        )[0]
        smart = client._smartrecruiters(
            {
                "content": [
                    {
                        "id": "four",
                        "name": "Software Engineering Intern",
                        "company": {"name": "Acme"},
                        "location": {"city": "Singapore", "country": "SG"},
                        "releasedDate": "2026-07-15T09:45:00Z",
                    }
                ]
            },
            "AcmeSG",
            "Acme",
        )[0]
        workday = client._workday(
            {
                "jobPostingInfo": {
                    "id": "five",
                    "title": "Research Engineer (Machine Learning)",
                    "jobDescription": "<p>Build VLMs for autonomous mobile robots.</p>",
                    "location": "NTU Main Campus, Singapore",
                    "startDate": "2026-05-11",
                    "timeType": "Full time",
                    "jobReqId": "R0001",
                    "canApply": True,
                    "posted": True,
                    "externalUrl": "https://ntu.wd3.myworkdayjobs.com/Careers/job/example_R0001",
                }
            },
            "ntu.wd3.myworkdayjobs.com|Careers|/job/example_R0001",
            "Nanyang Technological University (NTU)",
            "https://ntu.wd3.myworkdayjobs.com/Careers/job/example_R0001",
        )[0]

        self.assertEqual(greenhouse["source_published_at"], "")
        self.assertEqual(greenhouse["source_updated_at"], "2026-07-15T09:00:00Z")
        self.assertTrue(lever["source_published_at"].endswith("Z"))
        self.assertEqual(ashby["source_published_at"], "2026-07-15T09:30:00Z")
        self.assertIn("Singapore", smart["location"])
        self.assertEqual(workday["source_published_at"], "2026-05-11T00:00:00Z")
        self.assertEqual(workday["ats_source_id"], "R0001")
        self.assertIn("autonomous mobile robots", workday["extracted_content"])

    def test_first_poll_baselines_old_jobs_then_alerts_new_entry(self) -> None:
        old = {
            "source_key": "LEVER:acme:old",
            "ats_provider": "LEVER",
            "ats_board": "acme",
            "ats_source_id": "old",
            "title": "Robotics Intern",
            "company": "Acme",
            "location": "Singapore",
            "link": "https://jobs.lever.co/acme/old",
            "snippet": "Robotics internship",
            "extracted_content": "Robotics internship for a current student.",
            "source_published_at": "2026-01-01T00:00:00Z",
            "source_updated_at": "",
            "employment_type": "Intern",
            "remote_type": "On-site",
        }
        new = {
            **old,
            "source_key": "LEVER:acme:new",
            "ats_source_id": "new",
            "title": "Embedded Software Intern",
            "link": "https://jobs.lever.co/acme/new",
            "snippet": "Embedded software internship",
            "extracted_content": "Embedded software internship using C and sensors.",
            "source_published_at": "2026-07-15T09:59:30Z",
        }
        fixed_now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "rapid.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "interval_seconds": 120,
                        "initial_alert_window_minutes": 0,
                        "max_alerts_per_poll": 5,
                        "minimum_quality_score": 45,
                        "auto_discover_boards": False,
                        "boards": [{"provider": "LEVER", "board": "acme", "company": "Acme"}],
                    }
                ),
                encoding="utf-8",
            )
            watcher = RapidJobWatcher(
                config_path=config_path,
                state_path=root / "state.json",
                status_path=root / "status.json",
                json_path=root / "jobs.json",
                csv_path=root / "jobs.csv",
                md_path=root / "jobs.md",
                store=CareerStore(root / "career.db"),
                feed_client=FakeFeedClient([[old], [old, new]]),
                row_loader=lambda: [],
                availability_checker=open_check,
                scorer=good_score,
                clock=lambda: fixed_now,
            )

            first = watcher.poll()
            second = watcher.poll()

            self.assertEqual(first["alerts_ready"], 0)
            self.assertEqual(first["baseline_entries_added"], 1)
            self.assertEqual(second["alerts_ready"], 1)
            self.assertEqual(second["new_jobs"][0]["title"], "Embedded Software Intern")
            self.assertEqual(second["new_jobs"][0]["agent_can_submit"], "NO")
            self.assertEqual(second["new_jobs"][0]["freshness_label"], "POSTED 30 SECONDS AGO")
            saved = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)

    def test_newly_discovered_board_does_not_alert_its_old_backlog(self) -> None:
        old = {
            "source_key": "LEVER:newboard:old",
            "ats_provider": "LEVER",
            "ats_board": "newboard",
            "ats_source_id": "old",
            "title": "Robotics Intern",
            "company": "New Board",
            "location": "Singapore",
            "link": "https://jobs.lever.co/newboard/old",
            "snippet": "Robotics internship",
            "extracted_content": "Robotics internship",
            "source_published_at": "2026-01-01T00:00:00Z",
            "source_updated_at": "",
            "employment_type": "Intern",
            "remote_type": "On-site",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "rapid.json"
            state_path = root / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "initial_alert_window_minutes": 1440,
                        "auto_discover_boards": False,
                        "boards": [{"provider": "LEVER", "board": "newboard", "company": "New Board"}],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"schema_version": 1, "initialized": True, "sources": {}, "boards": {}}),
                encoding="utf-8",
            )
            watcher = RapidJobWatcher(
                config_path=config_path,
                state_path=state_path,
                status_path=root / "status.json",
                json_path=root / "jobs.json",
                csv_path=root / "jobs.csv",
                md_path=root / "jobs.md",
                store=CareerStore(root / "career.db"),
                feed_client=FakeFeedClient([[old]]),
                row_loader=lambda: [],
                availability_checker=open_check,
                scorer=good_score,
                clock=lambda: datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
            )

            result = watcher.poll()

            self.assertEqual(result["alerts_ready"], 0)
            self.assertEqual(result["baseline_entries_added"], 1)

    def test_transient_check_failure_remains_pending_for_retry(self) -> None:
        entry = {
            "source_key": "ASHBY:acme:new",
            "ats_provider": "ASHBY",
            "ats_board": "acme",
            "ats_source_id": "new",
            "title": "AI Intern",
            "company": "Acme",
            "location": "Singapore",
            "link": "https://jobs.ashbyhq.com/acme/new",
            "snippet": "AI internship",
            "extracted_content": "AI internship",
            "source_published_at": "2026-07-15T09:59:30Z",
            "source_updated_at": "",
            "employment_type": "Intern",
            "remote_type": "On-site",
        }
        attempts = {"count": 0}

        def flaky(row: dict, timeout: int):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary")
            return open_check(row, timeout)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "rapid.json"
            config_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "interval_seconds": 120,
                        "initial_alert_window_minutes": 30,
                        "auto_discover_boards": False,
                        "boards": [{"provider": "ASHBY", "board": "acme", "company": "Acme"}],
                    }
                ),
                encoding="utf-8",
            )
            watcher = RapidJobWatcher(
                config_path=config_path,
                state_path=root / "state.json",
                status_path=root / "status.json",
                json_path=root / "jobs.json",
                csv_path=root / "jobs.csv",
                md_path=root / "jobs.md",
                store=CareerStore(root / "career.db"),
                feed_client=FakeFeedClient([[entry], [entry]]),
                row_loader=lambda: [],
                availability_checker=flaky,
                scorer=good_score,
                clock=lambda: datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
            )

            first = watcher.poll()
            second = watcher.poll()

            self.assertEqual(first["alerts_ready"], 0)
            self.assertEqual(second["alerts_ready"], 1)
            self.assertEqual(attempts["count"], 2)


if __name__ == "__main__":
    unittest.main()
