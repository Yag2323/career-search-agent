from __future__ import annotations

import argparse
from pathlib import Path

from career_agent_common import send_telegram_file, send_telegram_message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", help="Message text to send")
    parser.add_argument("--file", help="File path to send")
    parser.add_argument("--caption", default="", help="Caption for file")
    args = parser.parse_args()
    if args.message:
        send_telegram_message(args.message)
        print("Telegram message sent.")
    elif args.file:
        send_telegram_file(Path(args.file), args.caption)
        print("Telegram file sent.")
    else:
        raise SystemExit("Use --message or --file")


if __name__ == "__main__":
    main()
