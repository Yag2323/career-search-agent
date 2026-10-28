from __future__ import annotations

from career_agent_common import LOGS_DIR, merged_env, redact_text


def main() -> None:
    changed = 0
    env = merged_env()
    for path in LOGS_DIR.rglob("*.log"):
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        sanitized = redact_text(original, env)
        if sanitized == original:
            continue
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(sanitized, encoding="utf-8")
        temporary.replace(path)
        changed += 1
    print(f"Sanitized log files: {changed}")


if __name__ == "__main__":
    main()
