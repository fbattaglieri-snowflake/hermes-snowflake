"""Keep complete image scanning a blocking, build-aware CI gate."""

import re
from pathlib import Path

import yaml


def test_complete_image_gate():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    )
    job = workflow["jobs"]["build"]
    scans = [step for step in job["steps"] if step.get("with", {}).get("scan-type") == "image"]
    assert len(scans) == 1
    scan = scans[0]
    options = scan["with"]
    assert options["exit-code"] == "1"
    assert options["ignore-unfixed"] == "false"
    assert set(options["severity"].split(",")) == {"HIGH", "CRITICAL"}
    assert not any(key.startswith("skip-") for key in options)
    assert not scan.get("continue-on-error", False)
    assert "steps.image.outcome == 'success'" in scan["if"]
    assert "!cancelled()" in scan["if"]
    assert any(step.get("id") == "image" for step in job["steps"])
    for section in workflow["jobs"].values():
        for step in section["steps"]:
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40}$", step["uses"])