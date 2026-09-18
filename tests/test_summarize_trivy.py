"""Tests for the Trivy report summarizer.

The summarizer runs with ``if: always()`` in CI, including after a scan that
failed or never wrote a report, so its tolerance of missing and malformed input
is part of its contract and is covered here.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import summarize_trivy  # noqa: E402


def write_report(directory: Path, payload: dict) -> Path:
    report = directory / "trivy.json"
    report.write_text(json.dumps(payload))
    return report


def test_findings_are_grouped_by_target_and_deduplicated(tmp_path):
    report = write_report(
        tmp_path,
        {
            "Results": [
                {
                    "Target": "image (ubuntu 24.04)",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-1",
                            "PkgName": "libfoo",
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                        },
                        # Trivy repeats a CVE once per advisory source; the
                        # reviewer must see it once.
                        {
                            "VulnerabilityID": "CVE-1",
                            "PkgName": "libfoo",
                            "InstalledVersion": "1.0",
                            "FixedVersion": "1.1",
                        },
                    ],
                },
                {
                    "Target": "Python",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2",
                            "PkgName": "setuptools",
                            "InstalledVersion": "70.3.0",
                            "FixedVersion": "78.1.1",
                            "PkgPath": "opt/venv/setuptools-70.3.0.dist-info/METADATA",
                        }
                    ],
                },
            ]
        },
    )

    rendered = summarize_trivy.render(report, "Findings")

    assert "2 finding(s) across 2 target(s)." in rendered
    assert rendered.count("CVE-1") == 1
    assert "image (ubuntu 24.04):" in rendered
    assert "CVE-2  setuptools 70.3.0  (fixed in 78.1.1)" in rendered
    assert "[opt/venv/setuptools-70.3.0.dist-info/METADATA]" in rendered


def test_missing_fixed_version_is_reported_as_unfixable(tmp_path):
    report = write_report(
        tmp_path,
        {
            "Results": [
                {
                    "Target": "image (ubuntu 24.04)",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-3",
                            "PkgName": "linux-libc-dev",
                            "InstalledVersion": "6.8.0-139.139",
                        }
                    ],
                }
            ]
        },
    )

    assert "NO FIX AVAILABLE" in summarize_trivy.render(report, "Findings")


def test_empty_report_states_there_are_no_findings(tmp_path):
    report = write_report(tmp_path, {"Results": []})
    assert "No findings." in summarize_trivy.render(report, "Findings")


def test_results_key_present_but_null_is_tolerated(tmp_path):
    # Trivy emits "Results": null when a scan matched nothing at all.
    report = write_report(tmp_path, {"Results": None})
    assert "No findings." in summarize_trivy.render(report, "Findings")


def test_missing_report_does_not_raise(tmp_path):
    rendered = summarize_trivy.render(tmp_path / "absent.json", "Findings")
    assert "did not produce output" in rendered


def test_malformed_report_does_not_raise(tmp_path):
    report = tmp_path / "trivy.json"
    report.write_text("{not json")
    assert "not valid JSON" in summarize_trivy.render(report, "Findings")


@pytest.mark.parametrize("payload", [{"Results": []}, {"Results": None}])
def test_step_summary_receives_the_rendered_output(tmp_path, monkeypatch, payload):
    report = write_report(tmp_path, payload)
    step_summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))

    assert summarize_trivy.main([str(report), "--title", "Blocking findings"]) == 0

    written = step_summary.read_text()
    assert "Blocking findings" in written
    assert written.startswith("```")


def test_command_line_entry_point_exits_zero_on_a_missing_report(tmp_path):
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(REPOSITORY_ROOT / "scripts" / "summarize_trivy.py"), "absent.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "did not produce output" in completed.stdout
