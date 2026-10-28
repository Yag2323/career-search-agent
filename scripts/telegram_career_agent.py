from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from career_agent_common import (
    JOBS_DIR,
    LOGS_DIR,
    SCRIPTS_DIR,
    STATE_DIR,
    answer_telegram_callback,
    clean,
    get_telegram_updates,
    load_jobs,
    merged_env,
    read_json,
    run_logged,
    send_telegram_message,
    tail_file,
    telegram_config,
    write_json,
)
from career_store import CareerStore
from career_targeting import is_target_degree_role, recent_enough_for_digest, recency_rank
from job_delivery import (
    claim_deliveries,
    filter_undelivered,
    finalize_deliveries,
    release_deliveries,
    was_delivered,
)
from rapid_job_watch import (
    RapidJobWatcher,
    load_watch_config,
    requeue_alert,
    status_summary,
    update_watch_config,
)

STATE_FILE = STATE_DIR / "telegram_bot_state.json"
LOCK_FILE = STATE_DIR / "telegram_career_agent.lock"
LOG_FILE = LOGS_DIR / "telegram_career_agent.log"
AVAILABILITY_SEARCH_STATUS = JOBS_DIR / "availability_search_status.json"
ATS_SEARCH_STATUS = STATE_DIR / "current_ats_search_status.json"
WATCH_RUN_LOCK = threading.Lock()
WATCH_WAKE_EVENT = threading.Event()
WATCHER: RapidJobWatcher | None = None

HELP_TEXT = """Career Search Agent Commands

SEARCH
Run the expanded Singapore search. It checks direct employer ATS feeds first, then searches official career sites, research institutions, graduate programmes, internships, apprenticeships, entry-level roles, and adjacent technical opportunities. Previously sent jobs are excluded.

LATEST
Send every unseen qualified job from the latest completed search. TOP10, TOP5, and DIGEST remain compatibility aliases and no longer impose a result cap.

PROVIDERS
Show configured search providers and the latest fallback path without exposing API keys.

WATCH START
Enable two-minute monitoring of learned Greenhouse, Lever, Ashby, SmartRecruiters, and Workable employer feeds. Heavy Workday and SuccessFactors board searches run in the expanded daily search.

WATCH STOP
Pause rapid monitoring while keeping the Telegram agent online.

WATCH STATUS
Show rapid-watch health, interval, board count, and latest scan.

WATCH NOW
Run one direct employer ATS scan immediately.

STATUS
Show search freshness, unseen-result count, delivery-history count, and watcher health.

STOP AGENT
Stop this Telegram polling process.
"""


def acquire_process_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+", encoding="ascii")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise RuntimeError("Another Telegram career-agent process already owns the process lock.") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    handle.seek(0)
    return handle


def normalize_command(text: str) -> str:
    value = text.strip()
    if value.startswith("/"):
        value = value[1:]
    value = re.sub(r"^(\w+)@\w+", r"\1", value)
    return clean(value)


def skip_existing_updates(env: dict[str, str]) -> int | None:
    data = get_telegram_updates(offset=None, timeout=0, env=env)
    results = data.get("result", [])
    if not results:
        return None
    return max(int(item["update_id"]) for item in results) + 1


def save_offset(offset: int | None) -> None:
    write_json(STATE_FILE, {"offset": offset, "updated_at": time.time()})


def score_int(row: dict[str, Any], key: str = "quality_score") -> int:
    try:
        return int(float(clean(row.get(key) or row.get("match_score"))))
    except (TypeError, ValueError):
        return 0


def latest_search_is_current(max_age_hours: int = 36) -> bool:
    candidates = [read_json(AVAILABILITY_SEARCH_STATUS, {}), read_json(ATS_SEARCH_STATUS, {})]
    for payload in candidates:
        if clean(payload.get("status")).upper() not in {"SUCCESS", "NO_RECENT_CURRENT_JOBS"}:
            continue
        try:
            completed = datetime.fromisoformat(clean(payload.get("completed_at")))
            if completed.tzinfo is None:
                completed = completed.astimezone()
            if datetime.now().astimezone() - completed <= timedelta(hours=max_age_hours):
                return True
        except (TypeError, ValueError):
            continue
    return False


def availability_check_is_current(row: dict[str, Any], max_age_hours: int = 36) -> bool:
    try:
        checked = datetime.fromisoformat(clean(row.get("availability_checked_at")))
        if checked.tzinfo is None:
            checked = checked.astimezone()
        return datetime.now().astimezone() - checked <= timedelta(hours=max_age_hours)
    except (TypeError, ValueError):
        return False


def top_jobs(limit: int | None = None) -> list[dict[str, str]]:
    if not latest_search_is_current():
        return []
    rows = [
        row
        for row in load_jobs()
        if clean(row.get("availability_status")).upper() == "OPEN"
        and availability_check_is_current(row)
        and recent_enough_for_digest(row)
        and is_target_degree_role(row)[0]
        and score_int(row, "degree_fit_score") >= 55
    ]
    rows = filter_undelivered(rows)
    rows.sort(
        key=lambda row: (
            recency_rank(row),
            score_int(row, "degree_fit_score"),
            1 if clean(row.get("apply_type")).upper() == "DIRECT_APPLY" else 0,
            score_int(row),
        ),
        reverse=True,
    )
    return rows if limit is None else rows[: max(0, limit)]


def source_label(row: dict[str, Any]) -> str:
    if clean(row.get("apply_type")).upper() == "DIRECT_APPLY":
        return "Verified employer or ATS"
    if clean(row.get("apply_type")).upper() == "AGGREGATOR_ONLY":
        return "Live discovery listing"
    return "Live page requiring manual source review"


def rapid_alert_text(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            "NEW DEGREE-RELATED OPPORTUNITY",
            clean(row.get("freshness_label")) or "Newly detected in the latest employer ATS poll",
            "",
            f"{clean(row.get('job_id'))} - {clean(row.get('title'))}",
            f"Company: {clean(row.get('company'))}",
            f"Location: {clean(row.get('location'))}",
            f"Opportunity type: {clean(row.get('career_stage'))}",
            f"Degree fit: {clean(row.get('degree_fit_score'))}/100",
            f"Quality score: {clean(row.get('quality_score'))}",
            f"Source: {source_label(row)}",
            f"Availability: {clean(row.get('availability_status'))} ({clean(row.get('availability_confidence'))})",
            f"Published: {clean(row.get('source_published_at')) or 'Timestamp not supplied'}",
            f"Detected: {clean(row.get('detected_at'))}",
            f"Link: {clean(row.get('link'))}",
        ]
    )


def watcher_instance() -> RapidJobWatcher:
    global WATCHER
    if WATCHER is None:
        WATCHER = RapidJobWatcher()
    return WATCHER


def run_rapid_watch_once(env: dict[str, str], *, force: bool, announce: bool) -> dict[str, Any] | None:
    if not WATCH_RUN_LOCK.acquire(blocking=False):
        if announce:
            send_telegram_message("A rapid ATS scan is already running.", env)
        return None
    try:
        result = watcher_instance().poll(force=force)
        delivered = 0
        suppressed = 0
        failures = 0
        for row in result.get("new_jobs") or []:
            if not isinstance(row, dict):
                continue
            if was_delivered(row):
                suppressed += 1
                continue
            channel = "RAPID_TELEGRAM_ALERT"
            claimed = claim_deliveries([row], channel=channel)
            if not claimed:
                suppressed += 1
                continue
            try:
                message_ids = send_telegram_message(rapid_alert_text(row), env)
                finalize_deliveries(claimed, channel=channel, message_ids=message_ids)
                delivered += 1
            except Exception:
                release_deliveries(claimed)
                failures += 1
                requeue_alert(clean(row.get("source_key")), "Telegram alert delivery failed; queued for retry.")
        if announce:
            send_telegram_message(
                "\n".join(
                    [
                        "Rapid ATS scan complete.",
                        f"Boards checked: {result.get('successful_boards', 0)}/{result.get('board_count', 0)}",
                        f"New unseen opportunities delivered: {delivered}",
                        f"Previously delivered opportunities suppressed: {suppressed}",
                        f"Delivery retries queued: {failures}",
                        f"Board errors: {result.get('failed_boards', 0)}",
                    ]
                ),
                env,
            )
        return result
    finally:
        WATCH_RUN_LOCK.release()


def rapid_watch_loop(env: dict[str, str], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            config = load_watch_config()
            if config["enabled"]:
                run_rapid_watch_once(env, force=False, announce=False)
            delay = config["interval_seconds"] if config["enabled"] else 60
        except Exception as exc:
            print(f"Rapid watcher error: {exc}")
            delay = 60
        WATCH_WAKE_EVENT.wait(delay)
        WATCH_WAKE_EVENT.clear()


def provider_status_text(env: dict[str, str]) -> str:
    provider_keys = [
        ("TAVILY", "TAVILY_API_KEY"),
        ("SERPAPI", "SERPAPI_KEY"),
        ("JINA", "JINA_API_KEY"),
        ("FIRECRAWL", "FIRECRAWL_API_KEY"),
        ("APIFY", "APIFY_TOKEN"),
    ]
    configured = [name for name, key in provider_keys if clean(env.get(key))]
    latest = read_json(AVAILABILITY_SEARCH_STATUS, {})
    lines = [
        "Search Provider Status",
        "",
        "Configured: " + (", ".join(configured) or "None"),
        "Fallback order: TAVILY -> SERPAPI -> JINA -> FIRECRAWL -> APIFY",
        f"Latest search: {clean(latest.get('status')) or 'NEVER_RUN'}",
        f"Provider requests: {clean(latest.get('query_attempts')) or 'Not recorded'}",
        "Providers used: " + (", ".join(latest.get("providers_used") or []) or "Not recorded"),
    ]
    failures = latest.get("provider_failures") or []
    if failures:
        lines.append("Latest provider fallbacks:")
        for failure in failures[:5]:
            lines.append(f"- {clean(failure.get('provider'))}: {clean(failure.get('reason'))}")
    lines.append("Direct employer ATS monitoring does not consume search-provider credits.")
    return "\n".join(lines)


def watcher_status_text() -> str:
    status = status_summary()
    return "\n".join(
        [
            "Rapid Job Watch Status",
            "",
            f"State: {'RUNNING' if status['enabled'] else 'PAUSED'}",
            f"Poll interval: {status['interval_seconds']} seconds",
            f"Last result: {status['last_status']}",
            f"Last completed: {status['last_completed_at'] or 'Never'}",
            f"Next due: {status['next_poll_due_at'] or 'Not scheduled'}",
            f"Employer boards monitored: {status['board_count']}",
            f"Vacancies seen: {status['entries_seen']}",
            f"New alerts ready: {status['alerts_ready']}",
            f"Board errors: {status['failed_boards']}",
        ]
    )


def send_status(env: dict[str, str]) -> None:
    web = read_json(AVAILABILITY_SEARCH_STATUS, {})
    ats = read_json(ATS_SEARCH_STATUS, {})
    deliveries = CareerStore().list_job_deliveries()
    unseen = len(top_jobs()) if latest_search_is_current() else 0
    send_telegram_message(
        "\n".join(
            [
                "Career Search Agent Status",
                "",
                f"Latest broad search: {clean(web.get('status')) or 'NEVER_RUN'}",
                f"Latest broad search completed: {clean(web.get('completed_at')) or 'Never'}",
                f"Direct ATS unseen count: {clean(ats.get('current_ats_count')) or '0'}",
                f"Current unseen shortlist: {unseen}",
                f"Permanently suppressed delivered jobs: {len(deliveries)}",
                "",
                watcher_status_text(),
            ]
        ),
        env,
    )


def send_latest(env: dict[str, str]) -> None:
    run_logged(
        [sys.executable, str(SCRIPTS_DIR / "daily_digest.py"), "--telegram"],
        "Telegram command LATEST - send unseen jobs",
        LOG_FILE,
        env=env,
        timeout=180,
    )


def run_search_workflow(env: dict[str, str]) -> None:
    send_telegram_message(
        "Expanded search started. The full web-discovery query set and direct employer ATS boards will be checked, then every qualified unseen vacancy will be sent. This can take several minutes. Previously delivered jobs are excluded.",
        env,
    )
    result = run_logged(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPTS_DIR / "daily_search.ps1"),
            "-SkipTelegram",
        ],
        "Telegram command SEARCH - expanded search-only workflow",
        LOG_FILE,
        env=env,
        timeout=3600,
    )
    if "skipped because another run is active" in clean(result.stdout).lower():
        send_telegram_message("SEARCH was safely skipped because the scheduled search is already running.", env)
        return
    send_latest(env)


def handle_command(text: str, env: dict[str, str]) -> bool:
    command = normalize_command(text)
    upper = command.upper()
    if upper in {"START", "HELP"}:
        send_telegram_message(HELP_TEXT, env)
        return False
    if upper == "SEARCH":
        run_search_workflow(env)
        return False
    if upper in {"LATEST", "TOP10", "TOP5", "DIGEST"}:
        send_latest(env)
        return False
    if upper == "PROVIDERS":
        send_telegram_message(provider_status_text(env), env)
        return False
    if upper == "WATCH START":
        update_watch_config({"enabled": True})
        WATCH_WAKE_EVENT.set()
        run_rapid_watch_once(env, force=True, announce=True)
        return False
    if upper == "WATCH STOP":
        update_watch_config({"enabled": False})
        WATCH_WAKE_EVENT.set()
        send_telegram_message("Rapid employer-feed monitoring is paused.", env)
        return False
    if upper == "WATCH STATUS":
        send_telegram_message(watcher_status_text(), env)
        return False
    if upper == "WATCH NOW":
        run_rapid_watch_once(env, force=True, announce=True)
        return False
    if upper == "STATUS":
        send_status(env)
        return False
    if upper == "STOP AGENT":
        send_telegram_message("Career Search Agent stopping. Restart it from PowerShell when needed.", env)
        return True
    send_telegram_message("Unknown command.\n\n" + HELP_TEXT, env)
    return False


def handle_callback(callback: dict[str, Any], env: dict[str, str]) -> None:
    callback_id = str(callback.get("id", ""))
    if callback_id:
        answer_telegram_callback(callback_id, "This is now a search-only agent.", env)
    send_telegram_message("Application and resume buttons were removed. Send SEARCH or LATEST for new opportunities.", env)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search-only Telegram career agent.")
    parser.add_argument(
        "--process-existing",
        action="store_true",
        help="Process queued Telegram messages from before startup. Default skips old messages.",
    )
    args = parser.parse_args()

    process_lock = acquire_process_lock()
    watch_stop_event = threading.Event()
    watch_thread: threading.Thread | None = None
    try:
        env = merged_env()
        _token, chat_id = telegram_config(env)
        print("Career Search Agent Telegram bot starting.")
        print(f"Log file: {LOG_FILE}")

        state = read_json(STATE_FILE, {})
        offset = state.get("offset")
        if offset is None and not args.process_existing:
            offset = skip_existing_updates(env)
            save_offset(offset)

        watch_config = load_watch_config()
        send_telegram_message(
            "Career Search Agent is online in search-only mode. "
            f"Rapid employer-feed monitoring is {'running' if watch_config['enabled'] else 'paused'} "
            f"at {watch_config['interval_seconds']}-second intervals. Send HELP for commands.",
            env,
        )
        watch_thread = threading.Thread(
            target=rapid_watch_loop,
            args=(env, watch_stop_event),
            name="rapid-job-watch",
            daemon=True,
        )
        watch_thread.start()

        while True:
            try:
                data = get_telegram_updates(offset=offset, timeout=30, env=env)
                for update in data.get("result", []):
                    offset = int(update["update_id"]) + 1
                    save_offset(offset)
                    message: dict[str, Any] = update.get("message") or {}
                    callback: dict[str, Any] = update.get("callback_query") or {}

                    if callback:
                        callback_message = callback.get("message") or {}
                        incoming_chat = str((callback_message.get("chat") or {}).get("id", ""))
                        if incoming_chat == str(chat_id):
                            handle_callback(callback, env)
                        continue

                    incoming_chat = str((message.get("chat") or {}).get("id", ""))
                    incoming_text = clean(message.get("text"))
                    if incoming_chat != str(chat_id) or not incoming_text:
                        continue
                    try:
                        if handle_command(incoming_text, env):
                            return
                    except Exception as exc:
                        send_telegram_message(
                            f"Command failed: {exc}\n\nRecent log:\n{tail_file(LOG_FILE, lines=20)}",
                            env,
                        )
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"Polling error: {exc}")
                time.sleep(5)
    finally:
        watch_stop_event.set()
        WATCH_WAKE_EVENT.set()
        if watch_thread is not None:
            watch_thread.join(timeout=10)
        process_lock.close()


if __name__ == "__main__":
    main()
