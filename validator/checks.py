"""
checks.py
=========

The validation checks themselves.

Every check follows the same contract:

    def check_something(engine, table) -> CheckResult | list[CheckResult]

It receives a connected ValidationEngine and one TableSpec, runs SQL against
the src_/tgt_ views, and returns a verdict with evidence attached.

Checks are registered in CHECK_REGISTRY at the bottom of this file. The runner
iterates the registry, so adding a check means writing a function and adding
one line to the list -- nothing else in the codebase needs to know about it.

--------------------------------------------------------------------------
A NOTE ON WHY THE SQL LOOKS THE WAY IT DOES
--------------------------------------------------------------------------
Reconciliation SQL is mostly one of three shapes:

  1. AGGREGATE COMPARISON -- count/sum both sides, compare the numbers.
     Cheap, catches wholesale loss, tells you nothing about which rows.

  2. ANTI-JOIN -- LEFT JOIN ... WHERE parent IS NULL, or NOT EXISTS.
     Finds rows on one side with no match on the other. This is the workhorse
     of migration validation: missing rows, orphaned references, unmatched
     keys are all anti-joins.

  3. GROUP BY ... HAVING -- find keys that occur more often than they should.
     This is how duplicates are caught.

Almost every check below is one of those three.
"""

import pandas as pd

from .config import (
    NULL_RATE_DRIFT_TOLERANCE,
    VALUE_DIFF_SAMPLE_SIZE,
    ForeignKey,
    TableSpec,
)
from .engine import ValidationEngine
from .report import CheckResult, Status

# How many offending rows to pull back as evidence. We cap this because a
# badly broken migration could otherwise return millions of rows and the
# report is meant to be read by a person, not archived.
EVIDENCE_LIMIT = 50


# ===========================================================================
# CHECK 1 -- ROW COUNT RECONCILIATION                          [WORKED EXAMPLE]
# ===========================================================================

def check_row_counts(engine: ValidationEngine, table: TableSpec) -> CheckResult:
    """
    Did every row that left the source arrive in the target?

    This is the crudest possible check and it is deliberately first. It is
    cheap, it runs on any table without configuration, and wholesale row loss
    is both the most common migration defect and the most damaging one. If a
    migration drops 12 payments, every downstream balance is wrong.

    Shape: AGGREGATE COMPARISON.

    Note what this check does NOT tell you: it compares two totals, so it
    cannot distinguish "12 rows lost" from "20 lost and 8 spuriously created".
    That is what the primary key checks are for. Cheap checks narrow the
    problem; they do not diagnose it.
    """
    source_rows = engine.scalar(f"SELECT COUNT(*) FROM src_{table.name}")
    target_rows = engine.scalar(f"SELECT COUNT(*) FROM tgt_{table.name}")
    difference = target_rows - source_rows

    metrics = {
        "source_rows": source_rows,
        "target_rows": target_rows,
        "difference": difference,
    }

    if difference == 0:
        return CheckResult(
            check="row_count_reconciliation",
            table=table.name,
            status=Status.PASS,
            summary=f"{source_rows:,} rows in source and target",
            metrics=metrics,
        )

    # Word the summary so the direction of the problem is obvious at a glance.
    if difference < 0:
        summary = (
            f"{abs(difference):,} rows MISSING from target "
            f"(source {source_rows:,}, target {target_rows:,})"
        )
    else:
        summary = (
            f"{difference:,} UNEXPECTED extra rows in target "
            f"(source {source_rows:,}, target {target_rows:,})"
        )

    return CheckResult(
        check="row_count_reconciliation",
        table=table.name,
        status=Status.FAIL,
        summary=summary,
        offender_count=abs(difference),
        metrics=metrics,
    )


# ===========================================================================
# CHECK 2 -- ORPHANED FOREIGN KEYS                             [WORKED EXAMPLE]
# ===========================================================================

def check_orphaned_foreign_keys(
    engine: ValidationEngine, table: TableSpec
) -> list[CheckResult]:
    """
    Does every foreign key in the target point at a row that actually exists?

    This is the check that catches the migration defect users notice first. A
    tenancy row survives, but the property it belongs to did not migrate, so
    the tenancy references a property_id that isn't there. Nothing errors. The
    row count might even reconcile. But open that tenancy in the application
    and the property details are blank.

    Shape: ANTI-JOIN. We look for child rows whose parent is absent.

    Returns a list because one table can have several foreign keys, and each
    deserves its own line in the report -- knowing that 'tenancies has 5 broken
    references' is much less useful than knowing which relationship broke.
    """
    results: list[CheckResult] = []

    if not table.foreign_keys:
        return results

    for fk in table.foreign_keys:
        results.append(_check_single_fk(engine, table, fk))

    return results


def _check_single_fk(
    engine: ValidationEngine, table: TableSpec, fk: ForeignKey
) -> CheckResult:
    """Run the anti-join for one specific foreign key relationship."""

    check_name = f"orphaned_fk[{fk.column}->{fk.parent_table}]"

    # The anti-join.
    #
    # LEFT JOIN keeps every child row, matched or not. Rows where the parent
    # side came back NULL are the ones with no matching parent -- the orphans.
    #
    # The `child.{fk.column} IS NOT NULL` filter matters: a NULL foreign key
    # is a *different* defect (a missing link, caught by the required-columns
    # check) and lumping the two together makes both harder to diagnose. A
    # NULL will always fail to join, so without this filter every NULL would
    # be miscounted as an orphan.
    count_sql = f"""
        SELECT COUNT(*)
        FROM tgt_{table.name}      AS child
        LEFT JOIN tgt_{fk.parent_table} AS parent
               ON child.{fk.column} = parent.{fk.parent_column}
        WHERE parent.{fk.parent_column} IS NULL
          AND child.{fk.column} IS NOT NULL
    """
    orphan_count = engine.scalar(count_sql)

    if orphan_count == 0:
        return CheckResult(
            check=check_name,
            table=table.name,
            status=Status.PASS,
            summary=f"all {fk.column} values resolve to a {fk.parent_table} row",
        )

    # Pull back examples. Note we select the child's own primary key too, so
    # whoever reads the report can go straight to the offending records.
    evidence_sql = f"""
        SELECT child.{table.primary_key},
               child.{fk.column} AS missing_{fk.parent_column}
        FROM tgt_{table.name}      AS child
        LEFT JOIN tgt_{fk.parent_table} AS parent
               ON child.{fk.column} = parent.{fk.parent_column}
        WHERE parent.{fk.parent_column} IS NULL
          AND child.{fk.column} IS NOT NULL
        ORDER BY child.{table.primary_key}
        LIMIT {EVIDENCE_LIMIT}
    """
    offenders = engine.query(evidence_sql)

    return CheckResult(
        check=check_name,
        table=table.name,
        status=Status.FAIL,
        summary=(
            f"{orphan_count:,} rows reference a {fk.parent_table} "
            f"that does not exist in the target"
        ),
        offenders=offenders,
        offender_count=orphan_count,
        metrics={"orphaned_rows": orphan_count},
    )


# ===========================================================================
# CHECK 3 -- PRIMARY KEY INTEGRITY                                  [YOUR TURN]
# ===========================================================================

def check_primary_key_integrity(
    engine: ValidationEngine, table: TableSpec
) -> CheckResult:
    """
    Is the primary key in the target unique and non-null?

    WHY IT MATTERS
    A migration tool that retries a batch can insert the same row twice. The
    application then shows duplicate records, and any join through that key
    silently multiplies row counts downstream -- a payments join against a
    duplicated tenancy will double the reported rent collected.

    SHAPE: GROUP BY ... HAVING, plus a null count.

    SKETCH -- duplicates:
        SELECT {pk}, COUNT(*) AS occurrences
        FROM tgt_{table}
        GROUP BY {pk}
        HAVING COUNT(*) > 1

    SKETCH -- nulls:
        SELECT COUNT(*) FROM tgt_{table} WHERE {pk} IS NULL

    THINGS TO DECIDE
    - Report duplicates and nulls as one result or two? One is simpler to read;
      two is easier to act on. Your call -- document whichever you choose.
    - The summary line should carry the count, not just "found duplicates".
      Compare "3 duplicate keys" against "3 duplicate keys affecting 47 rows":
      the second tells the reader how much damage there is.
    """
    raise NotImplementedError("Implement check_primary_key_integrity")


# ===========================================================================
# CHECK 4 -- DUPLICATE BUSINESS KEYS                                [YOUR TURN]
# ===========================================================================

def check_duplicate_business_keys(
    engine: ValidationEngine, table: TableSpec
) -> CheckResult:
    """
    Does the same real-world entity appear more than once under different IDs?

    WHY IT MATTERS -- AND WHY IT IS DIFFERENT FROM CHECK 3
    Check 3 catches the same primary key twice. This catches the same *person*
    twice under two different primary keys. Technically the table is perfectly
    valid; in reality one landlord now has two records, two statements, and
    two logins.

    This is the defining data quality problem of a roll-up acquiring agency
    after agency: the same landlord exists in three CRMs, and unless the
    migration deduplicates on a business key they arrive as three landlords.

    SHAPE: GROUP BY ... HAVING, on business_key rather than primary_key.

    SKETCH:
        SELECT {business_key_cols}, COUNT(*) AS occurrences
        FROM tgt_{table}
        GROUP BY {business_key_cols}
        HAVING COUNT(*) > 1

    THINGS TO DECIDE
    - Some tables have no business key defined. Return a PASS with a summary
      saying it was not applicable, or skip silently? Prefer being explicit --
      a check that quietly does nothing is worse than one that says so.
    - Case and whitespace. 'F.Moriarty@x.com' and 'fmoriarty@x.com  ' are the
      same landlord to a human. Do you normalise with LOWER(TRIM(...)) before
      grouping? Doing so catches more real duplicates; not doing so is more
      literal. Whichever you pick, say so in the docstring -- an interviewer
      is more likely to ask about this than about anything else in the file.
    - Should this be FAIL or WARN? Genuine duplicates are a blocker, but the
      check is a heuristic, so there is a real argument for WARN. Decide, and
      be able to defend it.
    """
    raise NotImplementedError("Implement check_duplicate_business_keys")


# ===========================================================================
# CHECK 5 -- NULL RATE DRIFT                                        [YOUR TURN]
# ===========================================================================

def check_null_rate_drift(
    engine: ValidationEngine, table: TableSpec
) -> list[CheckResult]:
    """
    Is a column substantially emptier after migration than before?

    WHY IT MATTERS
    This is the check that catches a silently unmapped column. If the source
    export has phone numbers for 95% of landlords and the target has them for
    55%, nothing errored -- the mapping just quietly dropped them. Row counts
    reconcile. Foreign keys resolve. The data is simply gone.

    SHAPE: AGGREGATE COMPARISON, per column, on a ratio rather than a count.

    SKETCH:
        SELECT COUNT(*) FILTER (WHERE col IS NULL) * 1.0 / COUNT(*)
        FROM src_{table}
        -- then the same against tgt_, and compare the two rates

    (COUNT(*) FILTER (WHERE ...) is standard SQL and DuckDB supports it. It is
    cleaner than SUM(CASE WHEN ... THEN 1 ELSE 0 END), which does the same job.)

    THINGS TO DECIDE
    - Compare only columns present in BOTH src_ and tgt_. Use engine.columns()
      to intersect them. A column that exists on one side only is a schema
      difference, which is a different finding.
    - Use NULL_RATE_DRIFT_TOLERANCE from config rather than hard-coding a
      threshold, so the tolerance is visible in one place.
    - Direction matters. Target emptier than source is a defect. Target FULLER
      than source usually means defaults were applied during migration -- worth
      a WARN, not a FAIL, because it is often intentional.
    - Returns a list: one result per drifted column.

    A PRACTICAL WARNING
    On a wide table this runs one aggregate per column, which is slow if done
    naively in a Python loop. You can compute every column's null count in a
    single SELECT with one FILTER expression per column. Worth doing, and worth
    mentioning in interview -- it shows you thought about cost, not just
    correctness.
    """
    raise NotImplementedError("Implement check_null_rate_drift")


# ===========================================================================
# CHECK 6 -- VALUE LEVEL DIFF                                       [YOUR TURN]
# ===========================================================================

def check_value_level_diff(
    engine: ValidationEngine, table: TableSpec
) -> CheckResult:
    """
    For rows that exist on both sides, did any values actually change?

    WHY IT MATTERS
    Everything above this point checks that rows and relationships survived.
    None of it checks that the *contents* are the same. A migration that
    mangles a currency conversion, truncates a string, or shifts a date by a
    timezone will pass every previous check. This is the one that catches it.

    SHAPE: INNER JOIN on the primary key, then compare columns.

    SKETCH:
        SELECT s.{pk}, s.amount AS src_amount, t.amount AS tgt_amount
        FROM src_{table} s
        JOIN tgt_{table} t ON s.{pk} = t.{pk}
        WHERE s.amount IS DISTINCT FROM t.amount

    IS DISTINCT FROM, NOT !=
    This is the detail worth getting right. In SQL, NULL != NULL is not true --
    it is NULL, which is not true, so the row is excluded. A plain != therefore
    silently misses every row where one side is NULL. IS DISTINCT FROM treats
    NULL as a comparable value and returns true when exactly one side is NULL.
    Using != here is a classic reconciliation bug that makes a broken migration
    look clean.

    THINGS TO DECIDE
    - Sample or full scan? VALUE_DIFF_SAMPLE_SIZE exists in config for this.
      Sampling makes the check cheap on a large migration but means it can
      miss defects. Be explicit in the report about which you did -- claiming
      "no value differences" after checking 500 of 2,000,000 rows would be
      misleading, and this employer is hiring specifically for the judgement
      not to do that.
    - Floats. If any column is floating point, exact comparison will produce
      false positives from representation error. Consider a tolerance, or
      round both sides, for numeric columns.
    - Which columns? All shared columns, or a configured subset? All is more
      thorough; a subset is faster and less noisy.
    """
    raise NotImplementedError("Implement check_value_level_diff")


# ===========================================================================
# REGISTRY
# ===========================================================================
#
# The runner iterates this list. Order matters only for readability of the
# report -- checks are independent of each other.
#
# Uncomment each entry as you implement it. Keeping unimplemented checks out
# of the registry means the tool always runs cleanly; a half-finished check
# that raises NotImplementedError mid-run would be reported as an ERROR, which
# is correct behaviour but unhelpful while you are still building.

CHECK_REGISTRY = [
    check_row_counts,
    check_orphaned_foreign_keys,
    # check_primary_key_integrity,
    # check_duplicate_business_keys,
    # check_null_rate_drift,
    # check_value_level_diff,
]


def run_all_checks(engine: ValidationEngine, spec: list[TableSpec]) -> list[CheckResult]:
    """
    Run every registered check against every table in the spec.

    Each check is wrapped in a try/except so that one failing check cannot
    abort the run. A check that raises is reported as ERROR -- deliberately
    NOT as PASS, because a check that did not run has validated nothing, and
    treating that as success is how bad data reaches production.
    """
    results: list[CheckResult] = []

    for table in spec:
        for check_fn in CHECK_REGISTRY:
            try:
                outcome = check_fn(engine, table)
            except Exception as exc:  # noqa: BLE001 -- we want every failure mode
                results.append(
                    CheckResult(
                        check=check_fn.__name__,
                        table=table.name,
                        status=Status.ERROR,
                        summary=f"check could not run: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            # Checks may return a single result or several.
            if isinstance(outcome, list):
                results.extend(outcome)
            elif outcome is not None:
                results.append(outcome)

    return results
