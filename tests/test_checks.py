"""
test_checks.py
==============

Tests for the validation checks.

The important idea here: a validation tool has to be tested against migrations
whose defects are known in advance. Otherwise you are trusting the tool to tell
you the truth about data you cannot independently verify -- which is exactly the
position the tool exists to get you out of.

So each test builds a tiny migration by hand, with one specific defect (or
none), and asserts that the check finds precisely that defect. Small fixtures
rather than the full demo dataset, because a test that fails should point at
one thing.

Run with:
    pytest -v
"""

import csv
from pathlib import Path

import duckdb
import pytest

from validator.checks import check_orphaned_foreign_keys, check_row_counts
from validator.config import ForeignKey, TableSpec
from validator.engine import ValidationEngine
from validator.report import Status


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a list of dicts to CSV, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_scenario(tmp_path: Path, source: dict[str, list[dict]],
                   target: dict[str, list[dict]]) -> tuple[Path, Path]:
    """
    Build a miniature migration on disk.

    source -- table name -> rows, written as CSVs (the 'before')
    target -- table name -> rows, loaded into DuckDB (the 'after')

    Passing different data for source and target is how a defect is injected:
    the test decides exactly what went wrong.
    """
    source_dir = tmp_path / "source"
    for name, rows in source.items():
        _write_csv(source_dir / f"{name}.csv", rows)

    target_db = tmp_path / "target.duckdb"
    con = duckdb.connect(str(target_db))
    for name, rows in target.items():
        columns = ", ".join(rows[0].keys())
        placeholders = ", ".join("?" for _ in rows[0])
        col_defs = ", ".join(
            f"{c} {'INTEGER' if isinstance(rows[0][c], int) else 'VARCHAR'}"
            for c in rows[0]
        )
        con.execute(f"CREATE TABLE {name} ({col_defs})")
        con.executemany(
            f"INSERT INTO {name} ({columns}) VALUES ({placeholders})",
            [tuple(r.values()) for r in rows],
        )
    con.close()

    return source_dir, target_db


# Minimal table specs used by the tests. Deliberately separate from the real
# MIGRATION_SPEC so that changing the production config cannot silently change
# what the tests are asserting.

WIDGET_SPEC = TableSpec(
    name="widgets",
    source_csv="widgets.csv",
    target_table="widgets",
    primary_key="widget_id",
)

PART_SPEC = TableSpec(
    name="parts",
    source_csv="parts.csv",
    target_table="parts",
    primary_key="part_id",
    foreign_keys=[ForeignKey("widget_id", "widgets", "widget_id")],
)


# ---------------------------------------------------------------------------
# CHECK 1 -- row count reconciliation
# ---------------------------------------------------------------------------

def test_row_counts_pass_when_identical(tmp_path):
    """A faithful migration reports PASS and no offenders."""
    rows = [{"widget_id": i, "name": f"w{i}"} for i in range(1, 6)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"widgets": rows}, target={"widgets": rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[WIDGET_SPEC]) as engine:
        result = check_row_counts(engine, WIDGET_SPEC)

    assert result.status is Status.PASS
    assert result.metrics["difference"] == 0


def test_row_counts_detects_missing_rows(tmp_path):
    """Rows lost in transit are reported as MISSING, with the correct count."""
    source_rows = [{"widget_id": i, "name": f"w{i}"} for i in range(1, 11)]
    target_rows = source_rows[:7]          # 3 rows never arrived

    source_dir, target_db = build_scenario(
        tmp_path, source={"widgets": source_rows}, target={"widgets": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[WIDGET_SPEC]) as engine:
        result = check_row_counts(engine, WIDGET_SPEC)

    assert result.status is Status.FAIL
    assert result.metrics["difference"] == -3
    assert "MISSING" in result.summary


def test_row_counts_detects_extra_rows(tmp_path):
    """
    Rows that appear from nowhere are also a defect.

    Worth testing separately from the missing case: a migration that retries a
    failed batch can insert rows twice, and a check that only looked for loss
    would call that a pass.
    """
    source_rows = [{"widget_id": i, "name": f"w{i}"} for i in range(1, 6)]
    target_rows = source_rows + [{"widget_id": 99, "name": "ghost"}]

    source_dir, target_db = build_scenario(
        tmp_path, source={"widgets": source_rows}, target={"widgets": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[WIDGET_SPEC]) as engine:
        result = check_row_counts(engine, WIDGET_SPEC)

    assert result.status is Status.FAIL
    assert result.metrics["difference"] == 1
    assert "UNEXPECTED" in result.summary


def test_row_counts_cannot_see_offsetting_errors(tmp_path):
    """
    A KNOWN LIMITATION, asserted deliberately.

    If a migration loses 2 rows and duplicates 2 others, the totals match and
    this check passes. That is not a bug -- an aggregate comparison genuinely
    cannot distinguish the two -- but it IS a limitation that has to be known
    and covered by the primary key checks.

    Writing the limitation down as a passing test means nobody later reads a
    green row-count check and concludes the migration is sound.
    """
    source_rows = [{"widget_id": i, "name": f"w{i}"} for i in range(1, 11)]
    target_rows = source_rows[:8] + [source_rows[0], source_rows[1]]  # -2, +2 dupes

    source_dir, target_db = build_scenario(
        tmp_path, source={"widgets": source_rows}, target={"widgets": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[WIDGET_SPEC]) as engine:
        result = check_row_counts(engine, WIDGET_SPEC)

    assert result.status is Status.PASS   # totals reconcile
    # ...even though the target is demonstrably wrong. Hence check 3.


# ---------------------------------------------------------------------------
# CHECK 2 -- orphaned foreign keys
# ---------------------------------------------------------------------------

def test_orphaned_fk_passes_when_all_parents_present(tmp_path):
    widgets = [{"widget_id": i, "name": f"w{i}"} for i in range(1, 4)]
    parts = [{"part_id": 1, "widget_id": 1}, {"part_id": 2, "widget_id": 3}]

    source_dir, target_db = build_scenario(
        tmp_path,
        source={"widgets": widgets, "parts": parts},
        target={"widgets": widgets, "parts": parts},
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[WIDGET_SPEC, PART_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, PART_SPEC)

    assert len(results) == 1
    assert results[0].status is Status.PASS


def test_orphaned_fk_detects_missing_parent(tmp_path):
    """A child row pointing at a parent that did not migrate is an orphan."""
    widgets_source = [{"widget_id": i, "name": f"w{i}"} for i in range(1, 4)]
    widgets_target = widgets_source[:2]          # widget 3 never migrated
    parts = [{"part_id": 1, "widget_id": 1},
             {"part_id": 2, "widget_id": 3}]     # ...but part 2 still points at it

    source_dir, target_db = build_scenario(
        tmp_path,
        source={"widgets": widgets_source, "parts": parts},
        target={"widgets": widgets_target, "parts": parts},
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[WIDGET_SPEC, PART_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, PART_SPEC)

    result = results[0]
    assert result.status is Status.FAIL
    assert result.offender_count == 1
    # Evidence must identify the offending row so an engineer can act on it.
    assert result.offenders.iloc[0]["part_id"] == 2


def test_null_foreign_key_is_not_counted_as_orphan(tmp_path):
    """
    A NULL foreign key is a different defect from a broken one.

    This is the subtle case. A NULL will never match in a join, so without an
    explicit IS NOT NULL filter every NULL would be miscounted as an orphan --
    inflating the count and pointing engineers at the wrong problem.
    """
    widgets = [{"widget_id": 1, "name": "w1"}]
    parts = [{"part_id": 1, "widget_id": 1},
             {"part_id": 2, "widget_id": None}]   # unlinked, not orphaned

    source_dir, target_db = build_scenario(
        tmp_path,
        source={"widgets": widgets, "parts": parts},
        target={"widgets": widgets, "parts": parts},
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[WIDGET_SPEC, PART_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, PART_SPEC)

    assert results[0].status is Status.PASS


def test_table_with_no_foreign_keys_returns_nothing(tmp_path):
    """Tables without relationships should produce no FK results at all."""
    widgets = [{"widget_id": 1, "name": "w1"}]
    source_dir, target_db = build_scenario(
        tmp_path, source={"widgets": widgets}, target={"widgets": widgets}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[WIDGET_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, WIDGET_SPEC)

    assert results == []


# ---------------------------------------------------------------------------
# CHECKS 3-6 -- to be written alongside the checks themselves
# ---------------------------------------------------------------------------
#
# Suggested cases, following the same pattern as above:
#
# check_primary_key_integrity
#   - passes on a clean target
#   - detects a duplicated primary key, and reports how many ROWS are affected
#     (not just how many keys)
#   - detects a NULL primary key
#
# check_duplicate_business_keys
#   - passes when every business key is unique
#   - detects the same email under two different landlord_ids
#   - decide and then TEST your normalisation choice: should 'A@x.com' and
#     'a@x.com ' be treated as the same landlord? Whatever you decide, a test
#     documents it.
#   - handles a table with no business key defined
#
# check_null_rate_drift
#   - passes when null rates match
#   - detects a column that is substantially emptier in the target
#   - does NOT fire on a drift smaller than the tolerance
#   - only compares columns present on both sides
#
# check_value_level_diff
#   - passes when values are identical
#   - detects a changed amount on a row present in both
#   - detects a value that became NULL in the target -- this is the test that
#     will fail if you use != instead of IS DISTINCT FROM, which is exactly
#     why it is worth writing.

@pytest.mark.skip(reason="written alongside check_primary_key_integrity")
def test_primary_key_integrity_detects_duplicates(tmp_path):
    ...
