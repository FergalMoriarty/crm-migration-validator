"""
engine.py
=========

Sets up the comparison environment.

The whole point of using DuckDB here is that it can query a CSV file sitting on
disk *as if it were a database table*, and query a real database at the same
time, in the same SQL statement. That means we can write:

    SELECT ... FROM src_landlords FULL OUTER JOIN tgt_landlords ...

where src_landlords is a CSV the legacy CRM exported, and tgt_landlords is a
table in the production database. No loading step, no staging tables, no ETL.

After setup, every check can assume two naming conventions exist:

    src_<table>   -- the source CSV export      (the 'before' picture)
    tgt_<table>   -- the migrated target table  (the 'after' picture)

Nothing in this file writes data anywhere. The validator is strictly read-only:
it inspects a migration that has already happened and reports on it. It never
cleans, fixes, or moves data. That decision belongs to a human.
"""

from pathlib import Path

import duckdb

from .config import MIGRATION_SPEC, TableSpec


class ValidationEngine:
    """
    Owns the DuckDB connection and the src_/tgt_ views the checks run against.

    Usage:
        engine = ValidationEngine(source_dir="data/source", target="data/target.duckdb")
        engine.connect()
        df = engine.query("SELECT COUNT(*) FROM src_landlords")
        engine.close()
    """

    def __init__(
        self,
        source_dir: str | Path,
        target: str,
        target_kind: str = "duckdb",
        spec: list[TableSpec] | None = None,
    ):
        """
        source_dir  -- folder containing the CSV files exported by the legacy CRM
        target      -- path to a DuckDB file, OR a Postgres connection string
        target_kind -- "duckdb" (default, zero setup) or "postgres"
        spec        -- which tables to wire up; defaults to the full MIGRATION_SPEC
        """
        self.source_dir = Path(source_dir)
        self.target = target
        self.target_kind = target_kind
        self.spec = spec if spec is not None else MIGRATION_SPEC
        self.con: duckdb.DuckDBPyConnection | None = None

    # -- setup ------------------------------------------------------------

    def connect(self) -> "ValidationEngine":
        """
        Open an in-memory DuckDB session and register every source and target
        table as a view.

        We use an in-memory database deliberately: this process is a read-only
        observer of two systems that live elsewhere. It should leave no trace.
        """
        self.con = duckdb.connect(":memory:")
        self._register_sources()
        self._register_target()
        return self

    def _register_sources(self) -> None:
        """
        Expose each source CSV as a view named src_<table>.

        read_csv_auto sniffs column names and types from the file itself, which
        is what we want here -- the whole point is to look at what the legacy
        system actually exported, not what we hoped it would export.

        all_varchar=false lets DuckDB infer real types (dates, numerics) so that
        comparisons against the typed target columns behave sensibly.
        """
        for table in self.spec:
            csv_path = self.source_dir / table.source_csv
            if not csv_path.exists():
                raise FileNotFoundError(
                    f"Source export missing for table {table.name!r}: {csv_path}"
                )
            self.con.execute(
                f"""
                CREATE OR REPLACE VIEW src_{table.name} AS
                SELECT * FROM read_csv_auto('{csv_path.as_posix()}', header=true)
                """
            )

    def _register_target(self) -> None:
        """
        Expose each migrated table as a view named tgt_<table>.

        Two target kinds are supported:

        duckdb   -- ATTACH a DuckDB file. This is the default because it makes
                    the repo clonable and runnable with no database to install.

        postgres -- ATTACH a live PostgreSQL database through DuckDB's postgres
                    extension. This is the mode that matters in production: the
                    target is the real application database. The SQL in every
                    check is identical either way, which is the advantage of
                    putting the abstraction at the view layer.
        """
        if self.target_kind == "duckdb":
            self.con.execute(f"ATTACH '{self.target}' AS tgt (READ_ONLY)")
        elif self.target_kind == "postgres":
            self.con.execute("INSTALL postgres; LOAD postgres;")
            self.con.execute(f"ATTACH '{self.target}' AS tgt (TYPE postgres, READ_ONLY)")
        else:
            raise ValueError(f"Unknown target_kind: {self.target_kind!r}")

        for table in self.spec:
            self.con.execute(
                f"""
                CREATE OR REPLACE VIEW tgt_{table.name} AS
                SELECT * FROM tgt.{table.target_table}
                """
            )

    # -- querying ---------------------------------------------------------

    def query(self, sql: str):
        """Run SQL and return the result as a pandas DataFrame."""
        if self.con is None:
            raise RuntimeError("Engine not connected. Call connect() first.")
        return self.con.execute(sql).df()

    def scalar(self, sql: str):
        """
        Run SQL expected to return exactly one value, and return that value.

        Convenience for count-style checks so they don't all have to write
        .iloc[0, 0] by hand.
        """
        result = self.con.execute(sql).fetchone()
        return result[0] if result else None

    def columns(self, view: str) -> list[str]:
        """
        List the column names of a view.

        Used by checks that need to compare like-for-like columns across source
        and target without knowing the schema in advance -- for example the
        null-rate drift check, which must only compare columns present in both.
        """
        rows = self.con.execute(f"DESCRIBE {view}").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    # Allow use as a context manager: `with ValidationEngine(...) as engine:`
    def __enter__(self) -> "ValidationEngine":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()
