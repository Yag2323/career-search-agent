from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
ENV_FILE = BASE / ".env"
SECURE_DIR = BASE / ".secrets"
SECURE_STORE = SECURE_DIR / "credentials.dpapi.json"

SENSITIVE_KEYS = {
    "APIFY_TOKEN",
    "FIRECRAWL_API_KEY",
    "JINA_API_KEY",
    "SERPAPI_KEY",
    "TAVILY_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
}


def read_env(path: Path = ENV_FILE) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return lines, values


def run_powershell(script: str, input_text: str) -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    user_profile = os.environ.get("USERPROFILE", str(Path.home()))
    powershell_env = os.environ.copy()
    powershell_env["PSModulePath"] = os.pathsep.join(
        [
            str(Path(user_profile) / "Documents" / "WindowsPowerShell" / "Modules"),
            str(Path(program_files) / "WindowsPowerShell" / "Modules"),
            str(Path(system_root) / "system32" / "WindowsPowerShell" / "v1.0" / "Modules"),
        ]
    )
    guarded_script = (
        "Import-Module \"$PSHOME\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1\" "
        "-ErrorAction Stop; "
        + script
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", guarded_script],
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
        env=powershell_env,
    )
    if result.returncode != 0:
        raise RuntimeError("Windows DPAPI operation failed: " + result.stderr.strip())
    return result.stdout.strip()


def encrypt_value(value: str) -> str:
    script = "$plain=[Console]::In.ReadToEnd(); $secure=ConvertTo-SecureString $plain -AsPlainText -Force; ConvertFrom-SecureString $secure"
    return run_powershell(script, value)


def decrypt_value(value: str) -> str:
    script = (
        "$encrypted=[Console]::In.ReadToEnd(); $secure=ConvertTo-SecureString $encrypted; "
        "$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure); "
        "try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}"
    )
    return run_powershell(script, value)


@lru_cache(maxsize=4)
def load_secure_values(path: Path = SECURE_STORE) -> dict[str, str]:
    if os.name != "nt" or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    output: dict[str, str] = {}
    for key, encrypted in (payload.get("values") or {}).items():
        if encrypted:
            output[key] = decrypt_value(str(encrypted))
    return output


def save_secure_values(values: dict[str, str], path: Path = SECURE_STORE) -> None:
    if os.name != "nt":
        raise RuntimeError("DPAPI secure storage is only available on Windows.")
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = {key: encrypt_value(value) for key, value in values.items() if value}
    path.write_text(
        json.dumps(
            {
                "format": "windows-dpapi-current-user",
                "note": "Decryptable only by the Windows user that created this file.",
                "values": encrypted,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    load_secure_values.cache_clear()


def set_secure_value(key: str, value: str) -> None:
    normalized = key.strip().upper()
    if normalized not in SENSITIVE_KEYS:
        raise ValueError(f"Unsupported secure key: {normalized}")
    secret = value.strip()
    if not secret:
        raise ValueError(f"Cannot store an empty value for {normalized}.")
    values = load_secure_values().copy()
    values[normalized] = secret
    save_secure_values(values)
    if load_secure_values().get(normalized) != secret:
        raise RuntimeError(f"Secure-storage verification failed for {normalized}.")


def migrate_env(remove_plaintext: bool = False) -> list[str]:
    lines, values = read_env()
    existing = load_secure_values()
    migrated: list[str] = []
    for key, value in values.items():
        if key in SENSITIVE_KEYS or any(marker in key.upper() for marker in ["TOKEN", "SECRET", "PASSWORD", "API_KEY"]):
            if value:
                existing[key] = value
                migrated.append(key)
    if not migrated and not existing:
        return []
    save_secure_values(existing)
    verified = load_secure_values()
    for key in migrated:
        if verified.get(key) != values.get(key):
            raise RuntimeError(f"Secure-storage verification failed for {key}; plaintext file was not changed.")

    if remove_plaintext:
        sanitized: list[str] = []
        removed: set[str] = set()
        for raw in lines:
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in migrated:
                    if key not in removed:
                        sanitized.append(f"# {key} is stored in .secrets/credentials.dpapi.json")
                        removed.add(key)
                    continue
            sanitized.append(raw)
        ENV_FILE.write_text("\n".join(sanitized).rstrip() + "\n", encoding="utf-8")
    return migrated


def status() -> dict[str, Any]:
    secure = load_secure_values()
    _lines, env_values = read_env()
    sensitive_plaintext = sorted(key for key in env_values if key in SENSITIVE_KEYS and env_values[key])
    return {
        "secure_store_exists": SECURE_STORE.exists(),
        "secure_keys": sorted(secure),
        "plaintext_sensitive_keys": sensitive_plaintext,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage per-user Windows DPAPI career-agent secrets.")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--remove-plaintext", action="store_true")
    setter = sub.add_parser("set")
    setter.add_argument("key", choices=sorted(SENSITIVE_KEYS))
    setter.add_argument(
        "--stdin",
        action="store_true",
        help="Read the secret from standard input instead of a hidden prompt.",
    )
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "migrate":
        keys = migrate_env(args.remove_plaintext)
        print("Migrated keys: " + (", ".join(keys) if keys else "none"))
        print(f"Secure store: {SECURE_STORE}")
        print(f"Plaintext removed: {args.remove_plaintext}")
    elif args.command == "set":
        value = sys.stdin.read() if args.stdin else getpass.getpass(f"Value for {args.key}: ")
        set_secure_value(args.key, value)
        print(f"Stored {args.key} in the Windows DPAPI secure store.")
    else:
        payload = status()
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
