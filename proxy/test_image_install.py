"""Guard the pinned installer, launcher environment and complete image scan."""

from pathlib import Path

import yaml


def test_pinned_install_and_scan():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "docker/hermes/Dockerfile").read_text()
    assert "${HERMES_GIT_SHA}/scripts/install.sh" in dockerfile
    assert "HERMES_INSTALLER_SHA256" in dockerfile
    assert '--commit "$HERMES_GIT_SHA" --force-commit' in dockerfile
    assert "VIRTUAL_ENV=" not in dockerfile
    assert 'grep -F "$VENV_PY" /usr/local/bin/hermes' in dockerfile
    assert 'readlink -f "$VENV_PY"' in dockerfile
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    scans = [
        step for step in workflow["jobs"]["build"]["steps"]
        if step.get("with", {}).get("scan-type") == "image"
    ]
    assert len(scans) == 1
    assert scans[0]["with"]["exit-code"] == "1"
    assert scans[0]["with"]["ignore-unfixed"] == "false"
    assert not any(key.startswith("skip-") for key in scans[0]["with"])
    assert "steps.image.outcome == 'success'" in scans[0]["if"]