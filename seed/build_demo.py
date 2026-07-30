"""
build_demo.py
=============

Builds a demonstration migration so the validator has something to validate.

It produces two things:

  data/source/*.csv     -- what the legacy agency CRM "exported"
  data/target.duckdb    -- what the migration tool "loaded"

The target is built as a FAITHFUL COPY of the source first, and then specific
defects are applied to it as explicit SQL statements. That ordering is
deliberate: it means the defects are listed in one place, in plain SQL, and a
reviewer can read exactly what was broken and check that the validator finds
precisely those things and nothing else.

Run:
    python -m seed.build_demo            # broken target (the interesting case)
    python -m seed.build_demo --clean    # faithful target (should fully pass)

The random seed is fixed, so every run produces identical data. Reconciliation
output that changes between runs would be impossible to trust or to test.
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

# Fixed seed: reproducibility is not optional for a validation tool.
RANDOM_SEED = 20260730

DATA_DIR = Path("data")
SOURCE_DIR = DATA_DIR / "source"
TARGET_DB = DATA_DIR / "target.duckdb"

N_LANDLORDS = 150
N_PROPERTIES = 300
N_TENANCIES = 350
N_PAYMENTS = 1500

FIRST_NAMES = [
    "James", "Sarah", "Michael", "Emma", "David", "Laura", "Paul", "Rachel",
    "Andrew", "Claire", "Stephen", "Fiona", "Mark", "Helen", "Peter", "Julie",
    "Colin", "Nicola", "Gareth", "Bethany", "Declan", "Roisin", "Owen", "Megan",
]
LAST_NAMES = [
    "Bennett", "Carroll", "Doherty", "Ellis", "Fletcher", "Gallagher", "Hughes",
    "Irvine", "Jennings", "Kavanagh", "Lawson", "Mitchell", "Nolan", "O'Brien",
    "Pryce", "Quinn", "Reynolds", "Sheridan", "Turner", "Underwood", "Vaughan",
    "Whelan", "Yates", "Ashcroft",
]
STREETS = [
    "Bridge Street", "Church Lane", "Victoria Road", "Mill Hill", "Station Road",
    "The Grove", "Albert Terrace", "Queens Park", "Elm Avenue", "Harbour View",
    "Kingsway", "Meadow Close", "Northfield Road", "Orchard Gardens",
]
CITIES = [
    ("Manchester", "M"), ("Leeds", "LS"), ("Bristol", "BS"), ("Sheffield", "S"),
    ("Nottingham", "NG"), ("Liverpool", "L"), ("Newcastle", "NE"), ("Belfast", "BT"),
]
PAYMENT_TYPES = ["rent", "rent", "rent", "rent", "deposit", "fee", "arrears"]
TENANCY_STATUS = ["active", "active", "active", "ended", "pending"]


# ---------------------------------------------------------------------------
# Source generation
# ---------------------------------------------------------------------------

def generate_source() -> dict[str, list[dict]]:
    """
    Build the four source tables as lists of dicts.

    This stands in for a CSV export out of a legacy CRM. The data is plausible
    rather than realistic -- the point is to exercise the checks, not to model
    the UK lettings market.
    """
    rng = random.Random(RANDOM_SEED)

    # -- landlords ----------------------------------------------------------
    landlords = []
    for i in range(1, N_LANDLORDS + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        landlords.append({
            "landlord_id": i,
            "first_name": first,
            "last_name": last,
            # Email is the business key, so it must be unique in a clean source.
            # The id suffix guarantees that.
            "email": f"{first.lower()}.{last.lower().replace(chr(39), '')}{i}@example.com",
            "phone": f"07{rng.randint(100000000, 999999999)}",
            "created_at": (date(2019, 1, 1) + timedelta(days=rng.randint(0, 2000))).isoformat(),
        })

    # -- properties ---------------------------------------------------------
    properties = []
    for i in range(1, N_PROPERTIES + 1):
        city, prefix = rng.choice(CITIES)
        properties.append({
            "property_id": i,
            "landlord_id": rng.randint(1, N_LANDLORDS),
            "address_line1": f"{rng.randint(1, 220)} {rng.choice(STREETS)}",
            "city": city,
            "postcode": f"{prefix}{rng.randint(1, 40)} {rng.randint(1, 9)}"
                        f"{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}{rng.choice('ABDEFGHJLNPQRSTUWXYZ')}",
            "bedrooms": rng.randint(1, 5),
            "monthly_rent": round(rng.uniform(550, 2400), 2),
        })

    # -- tenancies ----------------------------------------------------------
    tenancies = []
    for i in range(1, N_TENANCIES + 1):
        start = date(2021, 1, 1) + timedelta(days=rng.randint(0, 1500))
        rent = round(rng.uniform(550, 2400), 2)
        tenancies.append({
            "tenancy_id": i,
            "property_id": rng.randint(1, N_PROPERTIES),
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=365)).isoformat(),
            "rent_amount": rent,
            "deposit_amount": round(rent * 1.15, 2),
            "status": rng.choice(TENANCY_STATUS),
        })

    # -- payments -----------------------------------------------------------
    payments = []
    for i in range(1, N_PAYMENTS + 1):
        payments.append({
            "payment_id": i,
            "tenancy_id": rng.randint(1, N_TENANCIES),
            "payment_date": (date(2023, 1, 1) + timedelta(days=rng.randint(0, 900))).isoformat(),
            "amount": round(rng.uniform(400, 2600), 2),
            "payment_type": rng.choice(PAYMENT_TYPES),
        })

    return {
        "landlords": landlords,
        "properties": properties,
        "tenancies": tenancies,
        "payments": payments,
    }


def write_csvs(tables: dict[str, list[dict]]) -> None:
    """Write each table out as a CSV, exactly as a CRM export would arrive."""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        path = SOURCE_DIR / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"  wrote {path}  ({len(rows):,} rows)")


# ---------------------------------------------------------------------------
# Target construction
# ---------------------------------------------------------------------------

def build_target(clean: bool) -> None:
    """
    Create the target database as a copy of the source, then optionally break it.

    Building the target FROM the source CSVs means that, before defects are
    applied, the two sides are identical by construction. Any finding the
    validator reports on a --clean build is therefore a bug in the validator,
    not in the data. That makes --clean a genuine self-test.
    """
    if TARGET_DB.exists():
        TARGET_DB.unlink()

    con = duckdb.connect(str(TARGET_DB))

    for name in ("landlords", "properties", "tenancies", "payments"):
        csv_path = (SOURCE_DIR / f"{name}.csv").as_posix()
        con.execute(
            f"CREATE TABLE {name} AS "
            f"SELECT * FROM read_csv_auto('{csv_path}', header=true)"
        )
        print(f"  loaded target.{name}")

    if clean:
        con.close()
        print("\n  target built CLEAN -- validator should report no defects")
        return

    print("\n  injecting defects:")
    for label, description, statements in DEFECTS:
        for sql in statements:
            con.execute(sql)
        print(f"    [{label}] {description}")

    con.close()


# ---------------------------------------------------------------------------
# The defects
# ---------------------------------------------------------------------------
#
# Each entry is (label, human description, list of SQL statements).
#
# These are written as plain SQL rather than generated, so that a reviewer can
# read this list and know precisely what the validator is expected to catch.
# Every defect here maps to a check in validator/checks.py.

DEFECTS: list[tuple[str, str, list[str]]] = [

    # -> caught by check_row_counts (payments)
    ("D1", "12 payments dropped during load (row count loss)", [
        "DELETE FROM payments WHERE payment_id IN "
        "(11,97,143,288,401,555,672,830,944,1102,1287,1455)",
    ]),

    # -> caught by check_row_counts (properties) AND check_orphaned_foreign_keys
    #    (tenancies), because tenancies still reference these property_ids.
    ("D2", "4 properties never migrated, orphaning their tenancies", [
        "DELETE FROM properties WHERE property_id IN (17, 88, 201, 264)",
    ]),

    # -> caught by check_primary_key_integrity (properties)
    ("D3", "2 properties inserted twice (duplicate primary key)", [
        "INSERT INTO properties SELECT * FROM properties WHERE property_id IN (5, 150)",
    ]),

    # -> caught by check_duplicate_business_keys (landlords)
    #    Same email, different landlord_id: the same human being, twice.
    ("D4", "3 landlords duplicated under new IDs (same email)", [
        """INSERT INTO landlords
           SELECT landlord_id + 9000, first_name, last_name, email, phone, created_at
           FROM landlords WHERE landlord_id IN (12, 63, 118)""",
    ]),

    # -> caught by check_null_rate_drift (landlords.phone)
    ("D5", "landlord phone numbers silently dropped for ~40% of rows", [
        "UPDATE landlords SET phone = NULL WHERE landlord_id % 5 IN (0, 1)",
    ]),

    # -> caught by check_value_level_diff (payments.amount)
    #    Values changed on rows that still exist on both sides.
    ("D6", "7 payment amounts altered during load", [
        "UPDATE payments SET amount = ROUND(amount * 1.2, 2) "
        "WHERE payment_id IN (23, 199, 456, 701, 988, 1203, 1400)",
    ]),
]


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build the demo migration data.")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Build a faithful target with no defects (validator should fully pass).",
    )
    args = parser.parse_args()

    print("Generating source CSV export...")
    tables = generate_source()
    write_csvs(tables)

    print("\nBuilding target database...")
    build_target(clean=args.clean)

    print(f"\nDone. Source: {SOURCE_DIR}/   Target: {TARGET_DB}")


if __name__ == "__main__":
    main()
