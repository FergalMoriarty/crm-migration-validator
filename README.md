# migration-validator

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

The target is a DuckDB file by default so the repo is clonable and runnable
with no database to configure. Pointing it at a live PostgreSQL target is a
command line flag (`--target-kind postgres`); the check SQL is identical either
way.

---

## Quick start

```bash
pip install -r requirements.txt

# Build a demo migration with known defects deliberately injected
python -m seed.build_demo

# Validate it
python -m validator
```

Expected output — the tool finding the defects that were planted:

```
STATUS   TABLE          CHECK                            DETAIL
------------------------------------------------------------------------------
FAIL     landlords      row_count_reconciliation         3 UNEXPECTED extra rows in target
FAIL     properties     row_count_reconciliation         2 rows MISSING from target
PASS     properties     orphaned_fk[landlord_id->landlords] all landlord_id values resolve
PASS     tenancies      row_count_reconciliation         350 rows in source and target
FAIL     tenancies      orphaned_fk[property_id->properties] 7 rows reference a properties that does not exist
FAIL     payments       row_count_reconciliation         12 rows MISSING from target

VERDICT: REJECT -- blocking defects found. Do not deploy.
```

To confirm the validator isn't inventing problems, build a faithful target and
run it again:

```bash
python -m seed.build_demo --clean
python -m validator          # VERDICT: APPROVE
```

Because the clean target is built *from* the source CSVs, the two sides are
identical by construction — so any finding on a `--clean` run is a bug in the
validator, not in the data. That makes it a genuine self-test.

The exit code is `0` on approve and `1` on reject, so this can run in CI as a
gate on a migration pull request rather than as something someone has to
remember to look at.

---

## The checks

| # | Check | Shape | Catches | Status |
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
`validator/checks.py`, alongside the reasoning for each query.

Check 4 reports WARN rather than FAIL by design: it is a heuristic, and a false
positive that blocks a release is expensive. It surfaces candidates for a human
to judge rather than stopping the migration on the tool's own authority. Note
the consequence — a WARN does not affect the exit code, so this check alone
will not fail a CI gate.

### Verifying it against known defects

`seed/build_demo.py` injects six specific defects, listed there in plain SQL.
Each maps to exactly one check, so a broken run should catch all six:

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

### Why 3 and 4 are separate

Check 3 catches the same primary key twice. Check 4 catches the same *landlord*
twice, under two different primary keys. The second table is technically
valid — and completely wrong. In a roll-up, where the same landlord may exist
in three acquired CRMs, deduplicating on a business key is the whole problem.

---

## Known limitations

- **Row count reconciliation cannot see offsetting errors.** A migration that
  loses two rows and duplicates two others reconciles perfectly. That is
  inherent to comparing totals, not a bug — it is why checks 3 and 4 exist. The
  limitation is asserted as a passing test in `tests/test_checks.py` so that
  nobody reads a green row count and concludes the migration is sound.
- **Value-level diff can be sampled** rather than exhaustive on large tables,
  controlled by `VALUE_DIFF_SAMPLE_SIZE`. The report always states which was
  done — "no differences found" means something very different after a full
  scan than after sampling 500 rows out of two million.
- **Float comparison uses a tolerance** (`FLOAT_COMPARISON_TOLERANCE`), because
  exact equality on floating point reports representation artefacts as defects.
  A genuine difference smaller than the tolerance will not be reported.
- **Business key duplicate detection is a heuristic.** It finds entities that
  match on the configured key, after normalising case and whitespace. It cannot
  find the same landlord recorded under two different email addresses.
- **Null rate drift needs volume to be meaningful.** On a small table a single
  null can exceed the tolerance, so the threshold is better suited to tables of
  a few hundred rows and up.
- **Demo data is synthetic.** The schema models a UK lettings CRM migration
  (landlords → properties → tenancies → payments) but is generated, not real.

---

## Layout

```
validator/
  config.py    what is being validated — tables, keys, relationships
  engine.py    DuckDB setup; registers src_/tgt_ views
  checks.py    the checks themselves
  report.py    result objects and report rendering
  cli.py       command line entry point
seed/
  build_demo.py  generates source CSVs and a target DB, with defects listed in plain SQL
tests/
  test_checks.py  each check tested against a migration whose defects are known in advance
```

`config.py` is the file to read first. It declares the entire shape of the
migration — which tables move, what identifies a row, how tables relate —
without any check logic. Adding a table to the validation run means adding one
entry there.

---

## Testing

```bash
pytest -v
```

Each test builds a miniature migration by hand with one specific defect, then
asserts the check finds precisely that. A validation tool has to be tested
against data whose defects are known in advance — otherwise you are trusting it
to tell you the truth about data you cannot independently verify, which is the
position it exists to get you out of.

---

## Development notes

Built with AI-assisted development, under manual review.
