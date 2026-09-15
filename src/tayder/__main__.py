"""CLI entry: python -m tayder | tayder."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tayder", description="Discord-gated SPOT helper")
    parser.add_argument(
        "--dry-scan",
        action="store_true",
        help="One-shot strategy scan without Discord (needs public market API)",
    )
    args = parser.parse_args(argv)
    if args.dry_scan:
        from tayder.worker import run_paper_dry_scan

        run_paper_dry_scan()
        return
    from tayder.worker import Worker

    Worker().run()


if __name__ == "__main__":
    main(sys.argv[1:])
