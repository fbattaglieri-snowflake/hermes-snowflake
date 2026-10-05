"""Static guards for installation before dependency resolution and volume safety."""

from pathlib import Path


def test_installer_is_pinned_before_dependencies():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker/hermes/Dockerfile"
    ).read_text()
    assert "${HERMES_GIT_SHA}/scripts/install.sh" in dockerfile
    assert "HERMES_INSTALLER_SHA256" in dockerfile
    assert '--commit "$HERMES_GIT_SHA" --force-commit' in dockerfile
    assert "VIRTUAL_ENV=" not in dockerfile
    assert "git -C \"$REPO\" checkout" not in dockerfile
    assert 'grep -F "$VENV_PY" /usr/local/bin/hermes' in dockerfile
    assert 'readlink -f "$VENV_PY"' in dockerfile


def test_distro_copy_is_removed_through_package_manager():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker/hermes/Dockerfile"
    ).read_text()
    assert "apt-get purge -y python3-pip python3-setuptools" in dockerfile
    assert "pip==26.2.1" in dockerfile
    assert "setuptools-*.dist-info" not in dockerfile