"""
test_checks.py
==============

Tests for the validation checks.

The important idea: a validation tool has to be tested against migrations whose
defects are known in advance. Otherwise you are trusting the tool to tell you
the truth about data you cannot independently verify -- which is exactly the
position the tool exists to get you out of.

Every test follows the same three steps:

    ARRANGE  build a tiny migration where one specific thing is broken
    ACT      run one check against it
    ASSERT   confirm the check found precisely that thing

The sections below follow CHECK_REGISTRY in checks.py, which is the order the
report prints in.

The fixtures use landlords, properties and payments rather than abstract tables,
so the tests read in the same language as the rest of the repo. They are defined
locally rather than imported from config.py, so that changing the production
migration spec cannot silently change what these tests assert.

Nothing is mocked. Each test writes real CSVs and builds a real DuckDB database,
so the SQL itself is exercised -- a mocked test would prove the Python is
consistent with itself while saying nothing about whether the queries are right.

Run with:
    pytest -v
"""

import csv
from pathlib import Path

import duckdb

from validator.checks import (
    check_duplicate_business_keys,
    check_null_rate_drift,
    check_orphaned_foreign_keys,
    check_primary_key_integrity,
    check_row_counts,
    check_value_level_diff,
)
from validator.config import ForeignKey, TableSpec
from validator.engine import ValidationEngine
from validator.report import Status


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _sql_type(rows: list[dict], column: str) -> str:
    """
    Infer a DuckDB column type by finding the first non-None value.

    Scanning past None matters: if the first row happens to hold a NULL in a
    column, naive inference would type the whole column as VARCHAR and the
    numeric tests would silently end up comparing strings.
    """
    for row in rows:
        value = row[column]
        if value is None:
            continue
        if isinstance(value, bool):
            return "BOOLEAN"
        if isinstance(value, int):
            return "INTEGER"
        if isinstance(value, float):
            return "DOUBLE"
        return "VARCHAR"
    return "VARCHAR"


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write rows to CSV, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_scenario(
    tmp_path: Path,
    source: dict[str, list[dict]],
    target: dict[str, list[dict]],
) -> tuple[Path, Path]:
    """
    Build a miniature migration on disk.

    source -- table name -> rows, written as CSV files (the 'before' picture)
    target -- table name -> rows, loaded into DuckDB   (the 'after' picture)

    Passing different data for source and target is how a defect is injected:
    the test decides exactly what went wrong.
    """
    source_dir = tmp_path / "source"
    for name, rows in source.items():
        _write_csv(source_dir / f"{name}.csv", rows)

    target_db = tmp_path / "target.duckdb"
    con = duckdb.connect(str(target_db))
    for name, rows in target.items():
        columns = list(rows[0].keys())
        col_defs = ", ".join(f'"{c}" {_sql_type(rows, c)}' for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        con.execute(f"CREATE TABLE {name} ({col_defs})")
        con.executemany(
            f"INSERT INTO {name} VALUES ({placeholders})",
            [tuple(r[c] for c in columns) for r in rows],
        )
    con.close()

    return source_dir, target_db


# Minimal specs used by the tests.

LANDLORD_SPEC = TableSpec(
    name="landlords",
    source_csv="landlords.csv",
    target_table="landlords",
    primary_key="landlord_id",
    business_key=["email"],
)

PROPERTY_SPEC = TableSpec(
    name="properties",
    source_csv="properties.csv",
    target_table="properties",
    primary_key="property_id",
    foreign_keys=[ForeignKey("landlord_id", "landlords", "landlord_id")],
)

# A spec with no business key, to exercise that path explicitly.
NO_BUSINESS_KEY_SPEC = TableSpec(
    name="landlords",
    source_csv="landlords.csv",
    target_table="landlords",
    primary_key="landlord_id",
)

PAYMENT_SPEC = TableSpec(
    name="payments",
    source_csv="payments.csv",
    target_table="payments",
    primary_key="payment_id",
)


def landlord(landlord_id: int, email: str | None = None,
             phone: str | None = "07700900000") -> dict:
    """Build one landlord row. Keeps the tests short and the intent visible."""
    return {
        "landlord_id": landlord_id,
        "email": email if email is not None else f"landlord{landlord_id}@example.com",
        "phone": phone,
    }


def payment(payment_id: int, amount: float | None = 1000.00,
            ref: str | None = "REF") -> dict:
    """Build one payment row."""
    return {"payment_id": payment_id, "amount": amount, "reference": ref}


# ===========================================================================
# CHECK 1 -- row count reconciliation
# ===========================================================================

def test_row_counts_pass_when_identical(tmp_path):
    """A faithful migration reports PASS with zero difference."""
    rows = [landlord(i) for i in range(1, 6)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": rows}, target={"landlords": rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_row_counts(engine, LANDLORD_SPEC)

    assert result.status is Status.PASS
    assert result.metrics["difference"] == 0


def test_row_counts_detects_missing_rows(tmp_path):
    """Rows lost in transit are reported as MISSING, with the correct count."""
    source_rows = [landlord(i) for i in range(1, 11)]
    target_rows = source_rows[:7]                    # 3 rows never arrived

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_row_counts(engine, LANDLORD_SPEC)

    assert result.status is Status.FAIL
    assert result.metrics["difference"] == -3
    assert "MISSING" in result.summary


def test_row_counts_detects_extra_rows(tmp_path):
    """
    Rows that appear from nowhere are also a defect.

    Tested separately from the missing case because a migration that retries a
    failed batch inserts rows twice, and a check that only looked for loss would
    call that a pass.
    """
    source_rows = [landlord(i) for i in range(1, 6)]
    target_rows = source_rows + [landlord(99)]

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_row_counts(engine, LANDLORD_SPEC)

    assert result.status is Status.FAIL
    assert result.metrics["difference"] == 1
    assert "UNEXPECTED" in result.summary


def test_row_counts_cannot_see_offsetting_errors(tmp_path):
    """
    A KNOWN LIMITATION, asserted deliberately.

    If a migration loses 2 rows and duplicates 2 others, the totals match and
    this check passes. That is inherent to comparing aggregates, not a bug --
    but it IS a limitation, and it is why check 2 exists.

    Writing the limitation down as a passing test means nobody later reads a
    green row count and concludes the migration is sound.
    """
    source_rows = [landlord(i) for i in range(1, 11)]
    target_rows = source_rows[:8] + [source_rows[0], source_rows[1]]   # -2, +2

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_row_counts(engine, LANDLORD_SPEC)

    assert result.status is Status.PASS          # totals reconcile...
    # ...even though the target is demonstrably wrong. Hence check 2, below.


# ===========================================================================
# CHECK 2 -- primary key integrity
# ===========================================================================

def test_primary_key_integrity_passes_when_sound(tmp_path):
    rows = [landlord(i) for i in range(1, 6)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": rows}, target={"landlords": rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_primary_key_integrity(engine, LANDLORD_SPEC)

    assert result.status is Status.PASS
    assert result.metrics["duplicate_keys"] == 0
    assert result.metrics["null_keys"] == 0


def test_primary_key_integrity_detects_duplicates(tmp_path):
    """
    Catches the case row counts cannot: the same key appearing more than once.

    Note the two distinct numbers. Landlord 1 appears three times and landlord 2
    twice, so there are 2 duplicated KEYS affecting 5 ROWS. Reporting only one of
    those figures tells the reader either how widespread the problem is or how
    much data it touches, but not both.
    """
    source_rows = [landlord(i) for i in range(1, 4)]
    target_rows = [
        landlord(1), landlord(1), landlord(1),   # key 1 three times
        landlord(2), landlord(2),                # key 2 twice
        landlord(3),
    ]

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_primary_key_integrity(engine, LANDLORD_SPEC)

    assert result.status is Status.FAIL
    assert result.metrics["duplicate_keys"] == 2
    assert result.metrics["rows_affected_by_duplicates"] == 5
    # Most duplicated key is reported first, so the worst case is at the top.
    assert result.offenders.iloc[0]["duplicate_pk"] == 1
    assert result.offenders.iloc[0]["occurrences"] == 3


def test_primary_key_integrity_detects_nulls(tmp_path):
    """
    A null primary key identifies nothing and is its own defect.

    Worth a separate branch because the duplicate query filters nulls out --
    GROUP BY collapses all nulls into a single group, so a single null key would
    never appear as a duplicate and would go unreported without it.
    """
    source_rows = [landlord(1), landlord(2)]
    target_rows = [
        landlord(1),
        {"landlord_id": None, "email": "x@example.com", "phone": "07700900000"},
    ]

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_primary_key_integrity(engine, LANDLORD_SPEC)

    assert result.status is Status.FAIL
    assert result.metrics["null_keys"] == 1
    assert "null" in result.summary.lower()


# ===========================================================================
# CHECK 3 -- orphaned foreign keys
# ===========================================================================

def test_orphaned_fk_passes_when_all_parents_present(tmp_path):
    landlords = [landlord(i) for i in range(1, 4)]
    properties = [
        {"property_id": 1, "landlord_id": 1, "postcode": "BT1 1AA"},
        {"property_id": 2, "landlord_id": 3, "postcode": "BT2 2BB"},
    ]

    source_dir, target_db = build_scenario(
        tmp_path,
        source={"landlords": landlords, "properties": properties},
        target={"landlords": landlords, "properties": properties},
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[LANDLORD_SPEC, PROPERTY_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, PROPERTY_SPEC)

    assert len(results) == 1
    assert results[0].status is Status.PASS


def test_orphaned_fk_detects_missing_parent(tmp_path):
    """A property pointing at a landlord that did not migrate is an orphan."""
    landlords_source = [landlord(i) for i in range(1, 4)]
    landlords_target = landlords_source[:2]          # landlord 3 never migrated
    properties = [
        {"property_id": 1, "landlord_id": 1, "postcode": "BT1 1AA"},
        {"property_id": 2, "landlord_id": 3, "postcode": "BT2 2BB"},   # orphan
    ]

    source_dir, target_db = build_scenario(
        tmp_path,
        source={"landlords": landlords_source, "properties": properties},
        target={"landlords": landlords_target, "properties": properties},
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[LANDLORD_SPEC, PROPERTY_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, PROPERTY_SPEC)

    result = results[0]
    assert result.status is Status.FAIL
    assert result.offender_count == 1
    # Evidence must identify the offending row so an engineer can act on it.
    assert result.offenders.iloc[0]["property_id"] == 2


def test_null_foreign_key_is_not_counted_as_orphan(tmp_path):
    """
    A NULL foreign key is a different defect from a broken one.

    This is the subtle case. A NULL never matches in a join, so without the
    explicit IS NOT NULL filter every NULL would be miscounted as an orphan --
    inflating the count and pointing engineers at the wrong problem.
    """
    landlords = [landlord(1)]
    properties = [
        {"property_id": 1, "landlord_id": 1, "postcode": "BT1 1AA"},
        {"property_id": 2, "landlord_id": None, "postcode": "BT2 2BB"},  # unlinked
    ]

    source_dir, target_db = build_scenario(
        tmp_path,
        source={"landlords": landlords, "properties": properties},
        target={"landlords": landlords, "properties": properties},
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[LANDLORD_SPEC, PROPERTY_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, PROPERTY_SPEC)

    assert results[0].status is Status.PASS


def test_table_with_no_foreign_keys_returns_nothing(tmp_path):
    """Tables without relationships should produce no FK results at all."""
    landlords = [landlord(1)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": landlords}, target={"landlords": landlords}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        results = check_orphaned_foreign_keys(engine, LANDLORD_SPEC)

    assert results == []


# ===========================================================================
# CHECK 4 -- duplicate business keys
# ===========================================================================

def test_business_keys_pass_when_unique(tmp_path):
    rows = [landlord(i) for i in range(1, 6)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": rows}, target={"landlords": rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_duplicate_business_keys(engine, LANDLORD_SPEC)

    assert result.status is Status.PASS


def test_business_keys_detect_same_entity_under_two_ids(tmp_path):
    """
    The defining roll-up defect: one landlord, two records.

    The primary keys are perfectly unique, so check 2 passes. Only a business
    key comparison catches it -- which is why both checks exist.
    """
    source_rows = [landlord(1, email="jane@example.com"),
                   landlord(2, email="bob@example.com")]
    target_rows = source_rows + [landlord(9001, email="jane@example.com")]

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        pk_result = check_primary_key_integrity(engine, LANDLORD_SPEC)
        bk_result = check_duplicate_business_keys(engine, LANDLORD_SPEC)

    assert pk_result.status is Status.PASS            # keys are unique...
    assert bk_result.status is Status.WARN            # ...but the person is not
    assert bk_result.metrics["duplicate_entities"] == 1
    assert bk_result.metrics["rows_affected"] == 2


def test_business_keys_normalise_case_and_whitespace(tmp_path):
    """
    Documents a design decision, not just a behaviour.

    'Jane@Example.COM  ' and 'jane@example.com' are the same landlord to any
    human, and two acquired CRMs will happily store them differently. The check
    normalises with LOWER(TRIM(...)) before comparing, so they count as one
    entity.

    That is a deliberate trade: it catches more genuine duplicates at the cost
    of occasionally flagging records a purist would call distinct. If the
    decision is ever reversed, this test fails and forces the conversation.
    """
    source_rows = [landlord(1, email="jane@example.com")]
    target_rows = [
        landlord(1, email="jane@example.com"),
        landlord(2, email="  Jane@Example.COM  "),
    ]

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        result = check_duplicate_business_keys(engine, LANDLORD_SPEC)

    assert result.status is Status.WARN
    assert result.metrics["duplicate_entities"] == 1


def test_business_keys_not_applicable_without_a_key(tmp_path):
    """
    A check that quietly does nothing is worse than one that says it did nothing,
    because silence in a report reads as success.
    """
    rows = [landlord(i) for i in range(1, 4)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": rows}, target={"landlords": rows}
    )

    with ValidationEngine(source_dir, str(target_db),
                          spec=[NO_BUSINESS_KEY_SPEC]) as engine:
        result = check_duplicate_business_keys(engine, NO_BUSINESS_KEY_SPEC)

    assert result.status is Status.PASS
    assert "not applicable" in result.summary


# ===========================================================================
# CHECK 5 -- null rate drift
# ===========================================================================

def test_null_rate_drift_passes_when_stable(tmp_path):
    rows = [landlord(i) for i in range(1, 21)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": rows}, target={"landlords": rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        results = check_null_rate_drift(engine, LANDLORD_SPEC)

    assert len(results) == 1
    assert results[0].status is Status.PASS


def test_null_rate_drift_detects_silently_dropped_column(tmp_path):
    """
    The check that catches an unmapped column.

    Row counts reconcile. Keys are sound. Foreign keys resolve. The phone
    numbers are simply gone, because a mapping was missed -- and nothing else in
    the suite would notice.
    """
    source_rows = [landlord(i) for i in range(1, 101)]
    target_rows = [landlord(i, phone=None if i <= 40 else "07700900000")
                   for i in range(1, 101)]

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        results = check_null_rate_drift(engine, LANDLORD_SPEC)

    failures = [r for r in results if r.status is Status.FAIL]
    assert len(failures) == 1
    assert "phone" in failures[0].check
    assert failures[0].metrics["target_null_rate"] == "40.0%"


def test_null_rate_drift_ignores_small_changes(tmp_path):
    """
    Drift inside the tolerance is not reported.

    Without a tolerance, every migration that legitimately cleaned a handful of
    values would raise a failure -- and a report that cries wolf gets ignored,
    which is worse than no report at all.
    """
    source_rows = [landlord(i) for i in range(1, 101)]
    target_rows = [landlord(i, phone=None if i <= 3 else "07700900000")
                   for i in range(1, 101)]                       # 3% drift

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        results = check_null_rate_drift(engine, LANDLORD_SPEC)

    assert all(r.status is Status.PASS for r in results)


def test_null_rate_drift_warns_when_target_is_fuller(tmp_path):
    """
    Target fuller than source is a WARN, not a FAIL.

    It usually means defaults were applied during migration, which is often
    intentional -- but it is still a change to the data that someone should have
    agreed to.
    """
    source_rows = [landlord(i, phone=None if i <= 40 else "07700900000")
                   for i in range(1, 101)]
    target_rows = [landlord(i) for i in range(1, 101)]           # all populated

    source_dir, target_db = build_scenario(
        tmp_path, source={"landlords": source_rows}, target={"landlords": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[LANDLORD_SPEC]) as engine:
        results = check_null_rate_drift(engine, LANDLORD_SPEC)

    warnings = [r for r in results if r.status is Status.WARN]
    assert len(warnings) == 1
    assert "phone" in warnings[0].check


# ===========================================================================
# CHECK 6 -- value level diff
# ===========================================================================

def test_value_diff_passes_when_identical(tmp_path):
    rows = [payment(i) for i in range(1, 6)]
    source_dir, target_db = build_scenario(
        tmp_path, source={"payments": rows}, target={"payments": rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[PAYMENT_SPEC]) as engine:
        result = check_value_level_diff(engine, PAYMENT_SPEC)

    assert result.status is Status.PASS
    # The report must distinguish a full comparison from a sampled one, and
    # must state how many rows were actually compared rather than how many
    # existed in the source.
    assert "compared in full" in result.summary
    assert result.metrics["rows_compared"] == 5


def test_value_diff_detects_changed_amount(tmp_path):
    """
    The defect nothing else in the suite can see.

    Every row is present, keys are sound, relationships resolve -- and one
    payment is for the wrong amount.
    """
    source_rows = [payment(i, amount=1000.00) for i in range(1, 6)]
    target_rows = [payment(i, amount=1200.00 if i == 3 else 1000.00)
                   for i in range(1, 6)]

    source_dir, target_db = build_scenario(
        tmp_path, source={"payments": source_rows}, target={"payments": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[PAYMENT_SPEC]) as engine:
        result = check_value_level_diff(engine, PAYMENT_SPEC)

    assert result.status is Status.FAIL
    assert result.offender_count == 1
    row = result.offenders.iloc[0]
    assert row["payment_id"] == 3
    assert row["column_name"] == "amount"


def test_value_diff_detects_value_that_became_null(tmp_path):
    """
    THE TEST THAT JUSTIFIES 'IS DISTINCT FROM'.

    In SQL, NULL != NULL evaluates to NULL rather than TRUE, so a plain !=
    filters out every row where one side is NULL -- silently missing exactly the
    rows most worth catching. IS DISTINCT FROM treats NULL as comparable and
    returns TRUE when one side is NULL and the other is not.

    Swap the operator in checks.py and this test fails. That is its whole job.
    """
    source_rows = [payment(i, ref=f"REF{i}") for i in range(1, 6)]
    target_rows = [payment(i, ref=None if i == 2 else f"REF{i}") for i in range(1, 6)]

    source_dir, target_db = build_scenario(
        tmp_path, source={"payments": source_rows}, target={"payments": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[PAYMENT_SPEC]) as engine:
        result = check_value_level_diff(engine, PAYMENT_SPEC)

    assert result.status is Status.FAIL
    assert result.offender_count == 1
    assert result.offenders.iloc[0]["column_name"] == "reference"


def test_value_diff_tolerates_float_representation_noise(tmp_path):
    """
    Floating point equality is not a safe test.

    A difference of 0.001 on a currency column is representation noise, not a
    migration defect. Without a tolerance this check would fail on almost every
    real migration and be switched off within a week.
    """
    source_rows = [payment(i, amount=1000.00) for i in range(1, 6)]
    target_rows = [payment(i, amount=1000.001) for i in range(1, 6)]

    source_dir, target_db = build_scenario(
        tmp_path, source={"payments": source_rows}, target={"payments": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[PAYMENT_SPEC]) as engine:
        result = check_value_level_diff(engine, PAYMENT_SPEC)

    assert result.status is Status.PASS


def test_value_diff_only_compares_rows_present_on_both_sides(tmp_path):
    """
    Rows missing from the target are check 1's job, not this one's.

    The inner join means a row that never arrived simply is not compared.
    Keeping the concerns separate is what lets the report say '12 rows missing'
    and '7 values altered' rather than conflating them into one number that
    explains neither.
    """
    source_rows = [payment(i) for i in range(1, 11)]
    target_rows = [payment(i) for i in range(1, 6)]     # half never migrated

    source_dir, target_db = build_scenario(
        tmp_path, source={"payments": source_rows}, target={"payments": target_rows}
    )

    with ValidationEngine(source_dir, str(target_db), spec=[PAYMENT_SPEC]) as engine:
        result = check_value_level_diff(engine, PAYMENT_SPEC)

    assert result.status is Status.PASS
    assert result.metrics["rows_compared"] == 5