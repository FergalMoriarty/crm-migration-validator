"""
config.py
=========

This module declares WHAT we are validating, separately from HOW we validate it.

The idea is that a reviewer (or a future you) can open this one file and see the
entire shape of the migration -- which tables move, what identifies a row, and
how the tables relate -- without reading any check logic at all.

Adding a new table to the validation run means adding one TableSpec entry here.
It does not mean editing checks.py.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ForeignKey:
    """
    Describes a relationship that must survive the migration.

    Example: tenancies.property_id must point at a property_id that actually
    exists in the properties table. If the properties migration dropped rows,
    the tenancies rows pointing at them become 'orphaned' -- they reference a
    parent that isn't there. That is one of the most common and most damaging
    migration defects, because the data looks fine until someone opens a
    tenancy record and the property is blank.
    """
    column: str            # the column on THIS table, e.g. "property_id"
    parent_table: str      # the table it points at, e.g. "properties"
    parent_column: str     # the column on the parent, e.g. "property_id"


@dataclass(frozen=True)
class TableSpec:
    """
    One table in the migration.

    source_csv    -- the file the legacy CRM exported (the 'before' picture)
    target_table  -- the table in the Dwelly database (the 'after' picture)
    primary_key   -- the technical key. Must be unique and non-null in the target.
    business_key  -- the columns that identify a real-world entity.
    foreign_keys  -- relationships that must still resolve after migration.

    The distinction between primary_key and business_key matters a lot in a
    roll-up. Two rows can have different primary keys (because the migration
    tool assigned new IDs) while describing the SAME landlord. The primary key
    check will pass; only a business key check catches the duplicate.

    Every field here is read by at least one check. A declared-but-unenforced
    field is worse than no field at all: it reads as coverage that does not
    exist.
    """
    name: str
    source_csv: str
    target_table: str
    primary_key: str
    business_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The migration being validated.
#
# This models a UK lettings agency CRM export being migrated into a canonical
# property-management schema: landlords own properties, properties have
# tenancies, tenancies generate payments.
# ---------------------------------------------------------------------------

MIGRATION_SPEC: list[TableSpec] = [
    TableSpec(
        name="landlords",
        source_csv="landlords.csv",
        target_table="landlords",
        primary_key="landlord_id",
        # Email identifies a real landlord. If the same email appears twice
        # under two different landlord_ids, the migration created a duplicate
        # person -- they will get two sets of statements and two logins.
        business_key=["email"],
        foreign_keys=[],
    ),
    TableSpec(
        name="properties",
        source_csv="properties.csv",
        target_table="properties",
        primary_key="property_id",
        # A physical address identifies a property.
        business_key=["postcode", "address_line1"],
        foreign_keys=[
            ForeignKey("landlord_id", "landlords", "landlord_id"),
        ],
    ),
    TableSpec(
        name="tenancies",
        source_csv="tenancies.csv",
        target_table="tenancies",
        primary_key="tenancy_id",
        # One property can only have one tenancy starting on a given date.
        business_key=["property_id", "start_date"],
        foreign_keys=[
            ForeignKey("property_id", "properties", "property_id"),
        ],
    ),
    TableSpec(
        name="payments",
        source_csv="payments.csv",
        target_table="payments",
        primary_key="payment_id",
        business_key=["tenancy_id", "payment_date", "amount"],
        foreign_keys=[
            ForeignKey("tenancy_id", "tenancies", "tenancy_id"),
        ],
    ),
]


# Tolerance settings for checks that compare proportions rather than exact values.
#
# Null-rate drift needs a threshold because a small change is often legitimate
# (a column genuinely cleaned during migration), whereas a large one signals
# that a mapping was missed and the column silently stopped being populated.
NULL_RATE_DRIFT_TOLERANCE = 0.05     # 5 percentage points

# Value-level diff can run on a sample rather than every row, because on a large
# migration comparing every column of every row is slow and rarely necessary.
#
# Set to 0 to always compare every row. When the table is smaller than this
# figure the check performs a full comparison and says so in the report --
# "no differences found" means something very different after a full scan than
# after sampling 500 rows out of two million, and the report must not blur that.
VALUE_DIFF_SAMPLE_SIZE = 5000

# Floating point columns cannot be compared for exact equality: 0.1 + 0.2 is not
# 0.3 in binary floating point, so an exact test reports differences that are
# artefacts of representation rather than genuine migration defects. Two float
# values are treated as equal when they differ by less than this.
FLOAT_COMPARISON_TOLERANCE = 0.005


def get_table(name: str) -> TableSpec:
    """Look up a single TableSpec by name. Used by the tests."""
    for spec in MIGRATION_SPEC:
        if spec.name == name:
            return spec
    raise KeyError(f"No table named {name!r} in MIGRATION_SPEC")