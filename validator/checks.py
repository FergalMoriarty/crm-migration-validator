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
    FLOAT_COMPARISON_TOLERANCE,
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
            f"{orphan_count:,} rows reference a {fk.parent_table} row "
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

    IMPLEMENTATION NOTES

    Duplicates and nulls are reported as ONE result rather than two. Both are
    failures of the same property -- "this column identifies exactly one row" --
    and splitting them doubles the length of the report without telling the
    reader anything they could act on separately.

    The summary carries two numbers, not one: how many keys are duplicated, and
    how many rows that affects. "3 duplicate keys" and "3 duplicate keys
    affecting 47 rows" are very different situations to walk into.

    A note on the window function: COUNT(*) OVER (PARTITION BY pk) deliberately
    has NO ORDER BY. Adding one changes the default frame to
    RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW, which turns the whole
    partition total into a running count. Here it would happen to give the right
    answer -- ordering by the partition column makes every row a peer, and RANGE
    frames include peers -- but it would silently under-report the moment the
    ORDER BY column differed from the PARTITION BY column. An unordered window
    is the whole-partition aggregate we actually want.
    """
    pk = table.primary_key
    view = f"tgt_{table.name}"

    # -- nulls ------------------------------------------------------------
    null_count = engine.scalar(f'SELECT COUNT(*) FROM {view} WHERE "{pk}" IS NULL')

    # -- duplicates -------------------------------------------------------
    # ROW_NUMBER() lets us collapse each duplicated key to a single evidence
    # row (occurrence = 1) while COUNT(*) OVER still reports how many times the
    # key appears in total. One pass, both numbers.
    duplicates = engine.query(
        f"""
        WITH keyed AS (
            SELECT "{pk}"                                      AS pk,
                   ROW_NUMBER() OVER (PARTITION BY "{pk}")     AS occurrence,
                   COUNT(*)     OVER (PARTITION BY "{pk}")     AS occurrences
            FROM {view}
            WHERE "{pk}" IS NOT NULL
        )
        SELECT pk AS duplicate_pk, occurrences
        FROM keyed
        WHERE occurrences > 1
          AND occurrence = 1
        ORDER BY occurrences DESC, duplicate_pk
        """
    )

    duplicate_keys = len(duplicates)
    affected_rows = int(duplicates["occurrences"].sum()) if duplicate_keys else 0

    metrics = {
        "duplicate_keys": duplicate_keys,
        "rows_affected_by_duplicates": affected_rows,
        "null_keys": null_count,
    }

    if duplicate_keys == 0 and null_count == 0:
        return CheckResult(
            check="primary_key_integrity",
            table=table.name,
            status=Status.PASS,
            summary=f"{pk} is unique and non-null",
            metrics=metrics,
        )

    problems = []
    if duplicate_keys:
        problems.append(
            f"{duplicate_keys:,} duplicate {pk} values affecting {affected_rows:,} rows"
        )
    if null_count:
        problems.append(f"{null_count:,} rows with a null {pk}")

    return CheckResult(
        check="primary_key_integrity",
        table=table.name,
        status=Status.FAIL,
        summary="; ".join(problems),
        offenders=duplicates.head(EVIDENCE_LIMIT) if duplicate_keys else None,
        offender_count=affected_rows + null_count,
        metrics=metrics,
    )


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

    DECISIONS TAKEN, AND WHY

    1. Normalisation: business key values are compared as
       LOWER(TRIM(CAST(col AS VARCHAR))).

       'F.Moriarty@example.com' and 'fmoriarty@example.com  ' are the same
       landlord to any human being, and a migration that preserves the casing
       difference from two source CRMs would otherwise slip past. Normalising
       catches more genuine duplicates at the cost of occasionally flagging two
       records a purist would call distinct. On a roll-up, where the same
       landlord genuinely does exist in several acquired systems, that trade is
       worth making.

       CAST to VARCHAR first so composite keys mixing text, numbers and dates
       normalise through one code path.

    2. Status is WARN, not FAIL.

       This check is a heuristic. It cannot know that two records with the same
       email are truly the same person, and a false positive that blocks a
       migration is expensive. WARN puts it in front of a human without
       stopping the release on the tool's own judgement. Note the consequence:
       a WARN does not affect the exit code, so this check alone will not fail
       a CI gate. That is the intended behaviour -- deduplication is a decision
       for a person, not for a regex-equivalent.

    3. Tables with no business key return an explicit PASS saying so, rather
       than being skipped. A check that quietly does nothing is worse than one
       that says it did nothing, because silence reads as success.
    """
    if not table.business_key:
        return CheckResult(
            check="duplicate_business_keys",
            table=table.name,
            status=Status.PASS,
            summary="no business key configured -- check not applicable",
        )

    view = f"tgt_{table.name}"

    # Normalised expressions used for grouping, and the raw columns used for
    # evidence. The reader of the report needs to see the real values, not the
    # normalised ones.
    normalised = ", ".join(
        f'LOWER(TRIM(CAST("{col}" AS VARCHAR)))' for col in table.business_key
    )
    raw_columns = ", ".join(f'"{col}"' for col in table.business_key)

    duplicates = engine.query(
        f"""
        WITH keyed AS (
            SELECT {raw_columns},
                   "{table.primary_key}"                          AS example_id,
                   ROW_NUMBER() OVER (PARTITION BY {normalised})  AS occurrence,
                   COUNT(*)     OVER (PARTITION BY {normalised})  AS occurrences
            FROM {view}
        )
        SELECT {raw_columns}, occurrences
        FROM keyed
        WHERE occurrences > 1
          AND occurrence = 1
        ORDER BY occurrences DESC, {raw_columns}
        """
    )

    duplicate_entities = len(duplicates)
    affected_rows = int(duplicates["occurrences"].sum()) if duplicate_entities else 0
    key_description = " + ".join(table.business_key)

    metrics = {
        "business_key": key_description,
        "duplicate_entities": duplicate_entities,
        "rows_affected": affected_rows,
    }

    if duplicate_entities == 0:
        return CheckResult(
            check="duplicate_business_keys",
            table=table.name,
            status=Status.PASS,
            summary=f"no duplicate entities on ({key_description})",
            metrics=metrics,
        )

    return CheckResult(
        check="duplicate_business_keys",
        table=table.name,
        status=Status.WARN,
        summary=(
            f"{duplicate_entities:,} entities appear more than once on "
            f"({key_description}), affecting {affected_rows:,} rows"
        ),
        offenders=duplicates.head(EVIDENCE_LIMIT),
        offender_count=affected_rows,
        metrics=metrics,
    )


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

    IMPLEMENTATION NOTES

    - Only columns present on BOTH sides are compared. A column that exists on
      one side only is a schema difference, which is a different finding and
      would be misleading reported as drift.

    - Null counts for every column are computed in ONE aggregate per side, using
      one COUNT(*) FILTER expression per column, rather than looping in Python
      and issuing one query per column. On a wide table the difference is two
      queries against dozens.

    - Direction matters and is treated differently:
        target emptier than source  -> FAIL. Data was lost.
        target fuller than source   -> WARN. Usually defaults applied during
                                       migration, which is often intentional,
                                       but worth a human glance.
    """
    src_view = f"src_{table.name}"
    tgt_view = f"tgt_{table.name}"

    src_columns = engine.columns(src_view)
    tgt_columns = engine.columns(tgt_view)
    shared = [c for c in src_columns if c in tgt_columns]

    if not shared:
        return [
            CheckResult(
                check="null_rate_drift",
                table=table.name,
                status=Status.WARN,
                summary="source and target share no columns -- schema mismatch",
            )
        ]

    def null_rates(view: str) -> tuple[int, dict[str, int]]:
        """One query, one COUNT(*) FILTER per column."""
        expressions = ", ".join(
            f'COUNT(*) FILTER (WHERE "{c}" IS NULL) AS "null_{c}"' for c in shared
        )
        row = engine.query(f"SELECT COUNT(*) AS total_rows, {expressions} FROM {view}")
        total = int(row["total_rows"].iloc[0])
        nulls = {c: int(row[f"null_{c}"].iloc[0]) for c in shared}
        return total, nulls

    src_total, src_nulls = null_rates(src_view)
    tgt_total, tgt_nulls = null_rates(tgt_view)

    # An empty table on either side makes a rate meaningless.
    if src_total == 0 or tgt_total == 0:
        return [
            CheckResult(
                check="null_rate_drift",
                table=table.name,
                status=Status.WARN,
                summary=f"cannot compare null rates (source {src_total} rows, "
                        f"target {tgt_total} rows)",
            )
        ]

    results: list[CheckResult] = []

    for column in shared:
        src_rate = src_nulls[column] / src_total
        tgt_rate = tgt_nulls[column] / tgt_total
        drift = tgt_rate - src_rate

        if abs(drift) <= NULL_RATE_DRIFT_TOLERANCE:
            continue

        metrics = {
            "source_null_rate": f"{src_rate:.1%}",
            "target_null_rate": f"{tgt_rate:.1%}",
            "drift": f"{drift:+.1%}",
            "tolerance": f"{NULL_RATE_DRIFT_TOLERANCE:.1%}",
        }

        if drift > 0:
            results.append(CheckResult(
                check=f"null_rate_drift[{column}]",
                table=table.name,
                status=Status.FAIL,
                summary=(
                    f"{column} is emptier after migration: "
                    f"{src_rate:.1%} null in source, {tgt_rate:.1%} in target"
                ),
                offender_count=tgt_nulls[column] - src_nulls[column],
                metrics=metrics,
            ))
        else:
            results.append(CheckResult(
                check=f"null_rate_drift[{column}]",
                table=table.name,
                status=Status.WARN,
                summary=(
                    f"{column} is fuller after migration "
                    f"({src_rate:.1%} -> {tgt_rate:.1%} null); defaults applied?"
                ),
                metrics=metrics,
            ))

    if not results:
        return [
            CheckResult(
                check="null_rate_drift",
                table=table.name,
                status=Status.PASS,
                summary=f"null rates stable across {len(shared)} shared columns",
            )
        ]

    return results


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

    DECISIONS TAKEN, AND WHY

    1. Sampling. Controlled by VALUE_DIFF_SAMPLE_SIZE. When the source table is
       smaller than that figure the comparison is exhaustive, and the summary
       says "full". When it is larger, the summary says how many rows were
       sampled out of how many. "No value differences found" means something
       entirely different after a full scan than after sampling 500 rows out of
       two million, and the report must never blur the two.

    2. Floats. Exact equality on floating point produces differences that are
       artefacts of binary representation rather than migration defects, so
       numeric columns use an absolute tolerance (FLOAT_COMPARISON_TOLERANCE).
       NULLs are still handled by IS DISTINCT FROM before the tolerance applies,
       so a value that became NULL is always reported.

    3. All shared columns are compared, excluding the primary key -- it is the
       join condition, so by definition it cannot differ.

    WHY IS DISTINCT FROM AND NOT !=
    In SQL, NULL != NULL evaluates to NULL, not TRUE, so the row is filtered
    out. A plain != therefore silently misses every row where exactly one side
    is NULL -- precisely the rows most worth catching. IS DISTINCT FROM treats
    NULL as a comparable value and returns TRUE when one side is NULL and the
    other is not. Using != here is a classic reconciliation bug that makes a
    broken migration look clean.
    """
    pk = table.primary_key
    src_view = f"src_{table.name}"
    tgt_view = f"tgt_{table.name}"

    src_columns = engine.columns(src_view)
    tgt_columns = engine.columns(tgt_view)
    shared = [c for c in src_columns if c in tgt_columns and c != pk]

    if not shared:
        return CheckResult(
            check="value_level_diff",
            table=table.name,
            status=Status.WARN,
            summary="no shared non-key columns to compare",
        )

    # Column types drive the float handling. DESCRIBE returns one row per
    # column with its declared type.
    described = engine.query(f"DESCRIBE {src_view}")
    column_types = dict(zip(described["column_name"], described["column_type"]))
    float_types = {"FLOAT", "DOUBLE", "REAL"}

    def is_float(column: str) -> bool:
        declared = str(column_types.get(column, "")).upper()
        return any(t in declared for t in float_types) or declared.startswith("DECIMAL")

    # Decide sample vs full scan.
    source_rows = engine.scalar(f"SELECT COUNT(*) FROM {src_view}")
    sample_size = VALUE_DIFF_SAMPLE_SIZE
    sampled = bool(sample_size) and source_rows > sample_size

    sample_cte = (
        f'SELECT * FROM {src_view} ORDER BY "{pk}" LIMIT {sample_size}'
        if sampled else
        f"SELECT * FROM {src_view}"
    )

    # One SELECT per column, UNION ALLed together. This shape means the result
    # is already in long form -- one row per differing value -- which is what a
    # reader needs: the key, the column, and both values side by side.
    blocks = []
    for column in shared:
        if is_float(column):
            # IS DISTINCT FROM first so NULL transitions are always caught,
            # then the tolerance filter for genuine numeric drift.
            comparison = (
                f'(s."{column}" IS DISTINCT FROM t."{column}") '
                f'AND (s."{column}" IS NULL OR t."{column}" IS NULL '
                f'     OR ABS(CAST(s."{column}" AS DOUBLE) - CAST(t."{column}" AS DOUBLE)) '
                f'        > {FLOAT_COMPARISON_TOLERANCE})'
            )
        else:
            comparison = f's."{column}" IS DISTINCT FROM t."{column}"'

        blocks.append(
            f"""
            SELECT s."{pk}"                     AS {pk},
                   '{column}'                   AS column_name,
                   CAST(s."{column}" AS VARCHAR) AS source_value,
                   CAST(t."{column}" AS VARCHAR) AS target_value
            FROM sampled s
            JOIN {tgt_view} t ON s."{pk}" = t."{pk}"
            WHERE {comparison}
            """
        )

    union_sql = " UNION ALL ".join(blocks)

    rows_compared = engine.scalar(
        f"""
        WITH sampled AS ({sample_cte})
        SELECT COUNT(*) FROM sampled s JOIN {tgt_view} t ON s."{pk}" = t."{pk}"
        """
    )

    difference_count = engine.scalar(
        f"WITH sampled AS ({sample_cte}) SELECT COUNT(*) FROM ({union_sql})"
    )

    # Report the number of rows actually COMPARED, not the number of source
    # rows, because they differ whenever rows are missing from the target.
    # Saying "full comparison of 1,500 source rows" while only 1,488 could be
    # joined invites the reader to think 1,500 were checked. The unmatched rows
    # are check 1's finding, not this one's, but the wording must not obscure
    # that they went uncompared here.
    scope = (
        f"{rows_compared:,} matched rows compared, sampled from {source_rows:,}"
        if sampled else
        f"{rows_compared:,} of {source_rows:,} source rows compared in full"
    )

    metrics = {
        "rows_compared": rows_compared,
        "columns_compared": len(shared),
        "scope": scope,
    }

    if difference_count == 0:
        return CheckResult(
            check="value_level_diff",
            table=table.name,
            status=Status.PASS,
            summary=f"no value differences ({scope})",
            metrics=metrics,
        )

    offenders = engine.query(
        f"""
        WITH sampled AS ({sample_cte})
        SELECT * FROM ({union_sql})
        ORDER BY {pk}, column_name
        LIMIT {EVIDENCE_LIMIT}
        """
    )

    return CheckResult(
        check="value_level_diff",
        table=table.name,
        status=Status.FAIL,
        summary=f"{difference_count:,} values differ between source and target ({scope})",
        offenders=offenders,
        offender_count=difference_count,
        metrics=metrics,
    )


# ===========================================================================
# REGISTRY
# ===========================================================================
#
# The runner iterates this list. Order matters only for readability of the
# report -- checks are independent of each other.
#
# Order is chosen for readability of the report: cheap structural checks first
# (did the rows arrive, are the keys sound), then relationship and content
# checks. The checks are independent of one another, so the order has no effect
# on results.

CHECK_REGISTRY = [
    check_row_counts,
    check_primary_key_integrity,
    check_orphaned_foreign_keys,
    check_duplicate_business_keys,
    check_null_rate_drift,
    check_value_level_diff,
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
