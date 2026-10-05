#!/usr/bin/env python3
"""Render a Trivy JSON report as a deduplicated, reviewable finding list.

CI produces two reports per image. The blocking report is restricted to
vulnerabilities this repository can actually fix, and a finding there fails the
build. The advisory report covers the whole image, including upstream artifacts
we only consume, and never fails the build. Both are rendered by this script so
a reviewer reads the same shape of output either way.

Output is plain text on stdout and, when GITHUB_STEP_SUMMARY is set, the same
content is appended there so the findings are visible without opening the log.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterable, NamedTuple


class Finding(NamedTuple):
    """One vulnerability, reduced to the fields needed to act on it."""

    target: str
    vulnerability_id: str
    package: str
    installed_version: str
    fixed_version: str
    package_path: str

    def describe(self) -> str:
        """Render the finding as a single reviewable line."""
        remediation = f"fixed in {self.fixed_version}" if self.fixed_version else "NO FIX AVAILABLE"
        location = f" [{self.package_path}]" if self.package_path else ""
        return (
            f"  {self.vulnerability_id}  {self.package} {self.installed_version}"
            f"  ({remediation}){location}"
        )


def iter_findings(report: dict) -> Iterable[Finding]:
    """Yield every vulnerability in a parsed Trivy report.

    Trivy repeats a package once per CVE, and repeats a CVE once per advisory
    source, so the caller is expected to deduplicate.
    """
    for result in report.get("Results") or []:
        target = result.get("Target", "unknown")
        for vulnerability in result.get("Vulnerabilities") or []:
            yield Finding(
                target=target,
                vulnerability_id=vulnerability.get("VulnerabilityID", "UNKNOWN"),
                package=vulnerability.get("PkgName", "unknown"),
                installed_version=vulnerability.get("InstalledVersion", ""),
                fixed_version=vulnerability.get("FixedVersion", ""),
                package_path=vulnerability.get("PkgPath", ""),
            )


def format_report(findings: Iterable[Finding], title: str) -> str:
    """Group findings by target and render them under a heading."""
    grouped: dict[str, set[Finding]] = defaultdict(set)
    for finding in findings:
        grouped[finding.target].add(finding)

    lines = [f"### {title}", ""]
    if not grouped:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"

    total = sum(len(group) for group in grouped.values())
    lines.append(f"{total} finding(s) across {len(grouped)} target(s).")
    lines.append("")
    for target in sorted(grouped):
        lines.append(f"{target}:")
        lines.extend(finding.describe() for finding in sorted(grouped[target]))
        lines.append("")
    return "\n".join(lines) + "\n"


def render(report_path: Path, title: str) -> str:
    """Render the report at ``report_path``, tolerating its absence.

    The summarizing step runs with ``if: always()`` so that a failed scan is
    still explained. A scan that never produced a file must therefore not raise
    here, or the real failure would be masked by a traceback.
    """
    if not report_path.exists():
        return f"### {title}\n\nNo report at {report_path}; the scan did not produce output.\n"
    try:
        report = json.loads(report_path.read_text())
    except json.JSONDecodeError as error:
        return f"### {title}\n\nReport at {report_path} is not valid JSON: {error}\n"
    return format_report(iter_findings(report), title)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="path to the Trivy JSON report")
    parser.add_argument("--title", default="Image findings", help="heading for the rendered output")
    arguments = parser.parse_args(argv)

    rendered = render(arguments.report, arguments.title)
    print(rendered, end="")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"```\n{rendered}```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
