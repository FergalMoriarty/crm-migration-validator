"""
cli.py
======

Command line entry point.

    python -m validator --source data/source --target data/target.duckdb

The exit code is the important part: 0 when the migration is approvable, 1 when
it is not. That is what lets this run in CI as a gate on a migration pull
request rather than as something a person has to remember to look at.
"""

import argparse
import sys

from .checks import run_all_checks
from .config import MIGRATION_SPEC
from .engine import ValidationEngine
from .report import ValidationReport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validator",
        description="Validate a CRM data migration by reconciling source exports "
                    "against the migrated target database. Read-only.",
    )
    parser.add_argument("--source", default="data/source",
                        help="Directory containing the source CSV exports.")
    parser.add_argument("--target", default="data/target.duckdb",
                        help="Target DuckDB file, or a Postgres connection string.")
    parser.add_argument("--target-kind", default="duckdb", choices=["duckdb", "postgres"],
                        help="What kind of target to attach.")
    parser.add_argument("--markdown", action="store_true",
                        help="Emit Markdown instead of plain text (for PR comments).")
    args = parser.parse_args(argv)

    with ValidationEngine(
        source_dir=args.source,
        target=args.target,
        target_kind=args.target_kind,
    ) as engine:
        results = run_all_checks(engine, MIGRATION_SPEC)

    report = ValidationReport(results)
    print(report.to_markdown() if args.markdown else report.to_text())

    # 0 = approve, 1 = reject. CI reads this.
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
