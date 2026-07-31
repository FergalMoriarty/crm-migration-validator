"""
report.py
=========

Defines what a check produces, and how a run is presented to a human.

The output of this tool is a decision aid, not a fix. Someone has to look at it
and say "ship this migration" or "send it back". So the report is designed
around one question: if this fails, what do I need to see in order to act?

That means every failure carries three things:
  1. a count -- how bad is it
  2. a one-line summary -- what went wrong, in plain English
  3. example rows -- so the reader can go and look at real records

A failure with no examples is an accusation. A failure with examples is
evidence, which is what an engineer needs in order to fix the migration.
"""

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class Status(str, Enum):
    """
    PASS -- the check ran and found nothing wrong.
    FAIL -- the check found a defect that should block the migration.
    WARN -- something worth a human look, but not automatically a blocker.
    ERROR -- the check itself could not run (missing column, bad SQL).

    ERROR is deliberately distinct from FAIL. A check that crashes has NOT
    validated anything, and treating that as a pass would be the single most
    dangerous bug this tool could have.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    ERROR = "ERROR"


@dataclass
class CheckResult:
    """The outcome of a single check against a single table."""

    check: str                            # e.g. "row_count_reconciliation"
    table: str                            # e.g. "payments"
    status: Status
    summary: str                          # one line, human readable
    offenders: pd.DataFrame | None = None  # example rows that caused a failure
    offender_count: int = 0               # total offenders (may exceed rows shown)
    metrics: dict = field(default_factory=dict)  # structured numbers for later use

    @property
    def is_blocking(self) -> bool:
        """A migration should not be approved if anything failed or errored."""
        return self.status in (Status.FAIL, Status.ERROR)


class ValidationReport:
    """Collects CheckResults and renders them."""

    # How many example rows to print per failure. Enough to see the pattern,
    # few enough to keep the report readable in a terminal.
    MAX_EXAMPLE_ROWS = 5

    def __init__(self, results: list[CheckResult]):
        self.results = results

    # -- verdict ----------------------------------------------------------

    @property
    def passed(self) -> bool:
        """True only if nothing is blocking. This drives the CLI exit code."""
        return not any(r.is_blocking for r in self.results)

    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Status}
        for r in self.results:
            counts[r.status.value] += 1
        return counts

    # -- rendering --------------------------------------------------------

    def to_text(self) -> str:
        """Render the full report as plain text for the terminal."""
        lines: list[str] = []
        width = 78

        lines.append("=" * width)
        lines.append("CRM MIGRATION VALIDATION REPORT")
        lines.append("=" * width)
        lines.append("")

        # Summary table first, so a reader who only looks at the top of the
        # output still learns whether the migration is shippable.
        lines.append(f"{'STATUS':<8} {'TABLE':<14} {'CHECK':<32} {'DETAIL'}")
        lines.append("-" * width)
        for r in self.results:
            lines.append(
                f"{r.status.value:<8} {r.table:<14} {r.check:<32} {r.summary}"
            )
        lines.append("-" * width)
        lines.append("")

        # Then the evidence for anything that did not pass.
        problems = [r for r in self.results if r.status is not Status.PASS]
        if problems:
            lines.append("EVIDENCE")
            lines.append("=" * width)
            for r in problems:
                lines.append("")
                lines.append(f"[{r.status.value}] {r.table}.{r.check}")
                lines.append(f"  {r.summary}")
                if r.metrics:
                    for k, v in r.metrics.items():
                        lines.append(f"    {k}: {v}")
                if r.offenders is not None and not r.offenders.empty:
                    shown = r.offenders.head(self.MAX_EXAMPLE_ROWS)
                    lines.append("")
                    lines.append(f"  Example rows ({len(shown)} of {r.offender_count}):")
                    # Nulls are rendered as NULL, not Python's None: the reader
                    # is looking at database values and NULL is what they mean.
                    # fillna rather than to_string(na_rep=...) because pandas
                    # ignores na_rep on object-dtype columns, which is exactly
                    # what a mixed source/target value column ends up as.
                    shown = shown.fillna("NULL")
                    for line in shown.to_string(index=False).splitlines():
                        lines.append(f"    {line}")
            lines.append("")

        # Final verdict.
        counts = self.counts()
        lines.append("=" * width)
        lines.append(
            f"RESULT: {counts['PASS']} passed, {counts['FAIL']} failed, "
            f"{counts['WARN']} warnings, {counts['ERROR']} errors"
        )
        if self.passed:
            lines.append("VERDICT: APPROVE -- no blocking defects found.")
        else:
            lines.append("VERDICT: REJECT -- blocking defects found. Do not deploy.")
        lines.append("=" * width)

        return "\n".join(lines)

    def to_markdown(self) -> str:
        """
        Render as Markdown, for pasting into a pull request or ticket.

        This is the format that makes the tool usable inside a review workflow:
        run it in CI on every migration PR, post the output as a comment.
        """
        lines = ["# CRM Migration Validation Report", ""]
        verdict = "APPROVE" if self.passed else "REJECT"
        lines.append(f"**Verdict: {verdict}**")
        lines.append("")
        lines.append("| Status | Table | Check | Detail |")
        lines.append("|---|---|---|---|")
        for r in self.results:
            lines.append(f"| {r.status.value} | {r.table} | {r.check} | {r.summary} |")
        return "\n".join(lines)
