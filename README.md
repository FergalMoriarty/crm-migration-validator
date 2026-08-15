# crm-migration-validator

A read-only reconciliation tool for CRM data migrations.

When an acquired agency's CRM is migrated into a central platform, the question
that has to be answered before it reaches production is simple to ask and
tedious to answer: *did everything arrive, and did it arrive intact?*

This tool answers it with evidence. It reads the source export and the migrated
target, compares them, and issues a verdict — approve, or reject with the
offending rows attached.

It does not clean data. It does not move data. It does not write anything
anywhere. It inspects a migration that has already happened and reports on it.
The decision to ship stays with a person.

---

## How it works

The engine is [DuckDB](https://duckdb.org), which can query a CSV file on disk
as though it were a table and query a real database at the same time, in the
same statement. That means source and target can be compared directly:

```sql
SELECT ...
FROM   src_tenancies        -- a CSV the legacy CRM exported
LEFT JOIN tgt_properties    -- a table in the production database
       ON ...
```

No staging tables, no load step, nothing to install. Every check runs against
two naming conventions the engine sets up:

| View | What it is |
|---|---|
| `src_<table>` | the source CSV export — the *before* picture |
| `tgt_<table>` | the migrated target table — the *after* picture |

The target is a DuckDB file by default so the repo is clonable and runnable with
no database to configure. Pointing it at a live PostgreSQL target is a command
line flag; the check SQL is identical either way.

---

## Quick start

```bash
pip install -r requirements.txt

# Build a demo migration with known defects deliberately injected
python -m seed.build_demo

# Validate it
python -m validator
```

The demo target is deliberately broken, so the tool should reject it:

```
STATUS   TABLE          CHECK                            DETAIL
------------------------------------------------------------------------------
FAIL     landlords      row_count_reconciliation         3 UNEXPECTED extra rows in target (source 150, target 153)
PASS     landlords      primary_key_integrity            landlord_id is unique and non-null
WARN     landlords      duplicate_business_keys          3 entities appear more than once on (email), affecting 6 rows
FAIL     landlords      null_rate_drift[phone]           phone is emptier after migration: 0.0% null in source, 39.2% in target
FAIL     landlords      value_level_diff                 60 values differ between source and target (150 of 150 source rows compared in full)
FAIL     properties     row_count_reconciliation         2 rows MISSING from target (source 300, target 298)
FAIL     properties     primary_key_integrity            2 duplicate property_id values affecting 4 rows
PASS     properties     orphaned_fk[landlord_id->landlords] all landlord_id values resolve to a landlords row
WARN     properties     duplicate_business_keys          2 entities appear more than once on (postcode + address_line1), affecting 4 rows
...
FAIL     tenancies      orphaned_fk[property_id->properties] 7 rows reference a properties row that does not exist in the target
FAIL     payments       row_count_reconciliation         12 rows MISSING from target (source 1,500, target 1,488)
FAIL     payments       value_level_diff                 7 values differ between source and target (1,488 of 1,500 source rows compared in full)
------------------------------------------------------------------------------

RESULT: 13 passed, 8 failed, 2 warnings, 0 errors
VERDICT: REJECT -- blocking defects found. Do not deploy.
```

Every failure is followed by an evidence block listing example offending rows,
so a reader can go and look at real records rather than take the tool's word
for it.

To confirm the validator isn't inventing problems, build a faithful target and
run it again:

```bash
python -m seed.build_demo --clean
python -m validator          # VERDICT: APPROVE
```

Because the clean target is built *from* the source CSVs, the two sides are
identical by construction — so any finding on a `--clean` run is a bug in the
validator, not in the data. That makes it a genuine self-test.

---

## Running it against a real migration

```bash
python -m validator \
  --source /exports/agency-name/2026-08-04/ \
  --target "postgresql://readonly_user@db-host:5432/staging_db" \
  --target-kind postgres
```

Connect as a database user granted only `SELECT`. The tool never issues a write,
but the credential should enforce that rather than relying on the application to
behave — defence at the database level is stronger than defence in code.

The exit code is `0` on approve and `1` on reject, so this runs as a gate rather
than as something someone has to remember to look at:

```yaml
- name: Validate migration
  run: |
    python -m validator --source "$EXPORT_DIR" --target "$STAGING_DSN" \
      --target-kind postgres --markdown > report.md
```

The step fails on rejection and `report.md` can be posted as a pull request
comment. That is what `--markdown` is for.

---

## The checks

| # | Check | Shape | Catches | On failure |
|---|---|---|---|---|
| 1 | Row count reconciliation | aggregate comparison | wholesale row loss or duplication | FAIL |
| 2 | Primary key integrity | window functions | duplicated or null technical keys | FAIL |
| 3 | Orphaned foreign keys | anti-join | children whose parent never migrated | FAIL |
| 4 | Duplicate business keys | window functions | the same real entity under two IDs | WARN |
| 5 | Null rate drift | aggregate comparison | a column that silently stopped populating | FAIL / WARN |
| 6 | Value level diff | inner join + `IS DISTINCT FROM` | contents changed on rows that survived | FAIL |

Reconciliation SQL is almost always one of three shapes — an aggregate
comparison, an anti-join, or a partition-and-count. The checks are organised
around that, which is why adding a new one is short. All of the SQL lives in
`validator/checks.py`, alongside the reasoning for each query. The numbering
above follows `CHECK_REGISTRY` in that file, which is the order the report
prints in and the authority if anything ever disagrees.

Check 4 reports WARN rather than FAIL by design: it is a heuristic, and a false
positive that blocks a release is expensive. It surfaces candidates for a human
to judge rather than stopping the migration on the tool's own authority. Note
the consequence — a WARN does not affect the exit code, so this check alone will
not fail a CI gate.

### Why checks 2 and 4 are separate

Check 2 catches the same primary key appearing twice. Check 4 catches the same
*landlord* appearing twice, under two different primary keys. The second table
is technically valid — and completely wrong. In a roll-up, where the same
landlord may exist in three acquired CRMs, deduplicating on a business key is
the whole problem.

### Verifying it against known defects

`seed/build_demo.py` injects six specific defects, listed there in plain SQL.
Each maps to a check, so a broken run should catch all six:

| Defect | Caught by |
|---|---|
| D1 — 12 payments dropped | row count reconciliation |
| D2 — 4 properties never migrated | row counts + orphaned FK on tenancies |
| D3 — 2 properties inserted twice | primary key integrity |
| D4 — 3 landlords duplicated on email | duplicate business keys |
| D5 — landlord phone nulled for ~40% | null rate drift *and* value level diff |
| D6 — 7 payment amounts altered | value level diff |

D5 is deliberately caught twice, from two angles: as a statistical shift in how
often the column is populated, and as 60 specific rows whose phone number
changed. Two checks agreeing on a defect from different directions is a useful
property, not double-counting.

#### Read the properties row count carefully

D2 and D3 both land on `properties`. Four rows are deleted and two are inserted
twice, so the totals net to 300 → 298 and the report says **"2 rows MISSING"**.
That figure is arithmetically correct and practically misleading: four rows
were lost.

This is not a bug, and it is not a coincidence either — it is the offsetting-error
limitation below, occurring in the demo rather than only in a fixture. It is
also why the checks are layered. The row count nets to a wrong-looking number,
and then check 2 reports the two duplicated `property_id` values, and check 3
reports seven tenancies referencing properties that never arrived. No single
check describes the damage; three of them together do.

If you only ever run a row count, this is the shape of the thing you will miss.

---

## Known limitations

- **The migration spec is hardcoded.** `config.py` declares one schema —
  landlords, properties, tenancies, payments. Real use would need the spec
  loaded per source system, since every acquired agency exports a different
  shape. The change is small (`MIGRATION_SPEC` becomes a parsed YAML file behind
  a `--spec` flag) but it has not been made, and until it is, validating a
  second migration means editing Python.
- **Row count reconciliation cannot see offsetting errors.** A migration that
  loses two rows and duplicates two others reconciles perfectly. That is
  inherent to comparing totals, not a bug — it is why checks 2 and 4 exist. The
  limitation is asserted as a passing test in `tests/test_checks.py` so nobody
  reads a green row count and concludes the migration is sound, and it is
  visible in the demo output on the `properties` table.
- **`rows_compared` in the value diff counts join output rows, not distinct
  source rows.** When the target contains duplicate primary keys the join fans
  out: in the demo, `properties` reports "298 of 300 source rows compared" when
  296 distinct source rows actually matched, because the two duplicated
  `property_id` values each join twice. The verdict is unaffected and the
  duplication is never hidden — check 2 fails on the same table in the same
  report — but the label overstates coverage. `COUNT(DISTINCT s.<pk>)` would be
  the honest figure.
- **Value-level diff can be sampled** rather than exhaustive on large tables,
  controlled by `VALUE_DIFF_SAMPLE_SIZE`. The report always states which was
  done, and how many rows were actually compared — "no differences found" means
  something very different after a full scan than after sampling 500 rows out of
  two million.
- **Value-level diff only compares rows present on both sides.** Rows missing
  from the target are check 1's finding, not this one's; a row that never
  arrived is never compared.
- **Float comparison uses a tolerance** (`FLOAT_COMPARISON_TOLERANCE`), because
  exact equality on floating point reports representation artefacts as defects.
  A genuine difference smaller than the tolerance will not be reported.
- **Business key duplicate detection is a heuristic.** It finds entities that
  match on the configured key, after normalising case and whitespace. It cannot
  find the same landlord recorded under two different email addresses.
- **Null rate drift needs volume to be meaningful.** On a small table a single
  null can exceed the tolerance, so the threshold suits tables of a few hundred
  rows and up.
- **No incremental or delta support.** Each run compares a full export against a
  full target. Validating an ongoing sync rather than a one-off migration would
  need a different approach.
- **The PostgreSQL target path is untested end to end.** `--target-kind postgres`
  is implemented and the check SQL is identical either way, but every run to date
  has been against a DuckDB target. Schema qualification and identifier casing
  are the two things most likely to need adjusting on first contact with a real
  instance.
- **Demo data is synthetic.** The schema models a UK lettings CRM migration but
  is generated, not real.

---

## Layout

```
validator/
  config.py    what is being validated — tables, keys, relationships
  engine.py    DuckDB setup; registers src_/tgt_ views
  checks.py    the checks, and all of the SQL
  report.py    result objects and report rendering
  cli.py       command line entry point
seed/
  build_demo.py  generates source CSVs and a target DB, with defects in plain SQL
tests/
  test_checks.py  each check tested against a migration whose defects are known
```

`config.py` is the file to read first. It declares the entire shape of the
migration — which tables move, what identifies a row, how the tables relate —
without any check logic. Adding a table to the validation run means adding one
entry there. Every field it declares is read by at least one check: a
declared-but-unenforced field is worse than no field, because it reads as
coverage that does not exist.

---

## Testing

```bash
pytest -v
```

24 tests, four to five per check rather than one each. Every check gets a clean
case proving it does not invent problems, a planted-defect case proving it
catches what it exists for, and the edge cases around it — a NULL foreign key is
not an orphan, drift inside the tolerance should not fire, float representation
noise is not a defect.

Each test builds a miniature migration by hand with one specific defect, then
asserts the check finds precisely that. A validation tool has to be tested
against data whose defects are known in advance — otherwise you are trusting it
to tell you the truth about data you cannot independently verify, which is the
position it exists to get you out of.

Some of the tests exist to freeze a decision rather than to cover a branch:

- `test_value_diff_detects_value_that_became_null` fails the moment anyone
  replaces `IS DISTINCT FROM` with `!=`, which would silently skip every row
  where exactly one side is NULL.
- `test_business_keys_normalise_case_and_whitespace` forces a conversation
  before anyone removes the `LOWER(TRIM(...))` normalisation.
- `test_row_counts_cannot_see_offsetting_errors` asserts a **PASS** against a
  target that is demonstrably wrong — the limitation written down as executable
  text rather than as a bullet nobody reads.

Nothing is mocked: the tests write real CSVs and build a real database, so the
SQL itself is exercised rather than the Python around it.

---

## Development notes

Built with AI-assisted development, under manual review.