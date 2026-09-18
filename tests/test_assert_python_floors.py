"""Tests for the build-time Python distribution floor assertion.

The assertion runs inside the image build, where a false pass would let a
vulnerable second copy of a distribution ship and a false failure would block
the build for nothing. Both directions are covered here, against a synthetic
filesystem rather than a real interpreter tree.
"""

import importlib.util
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "docker" / "hermes" / "assert_python_floors.py"

_specification = importlib.util.spec_from_file_location("assert_python_floors", MODULE_PATH)
assert _specification and _specification.loader
assert_python_floors = importlib.util.module_from_spec(_specification)
sys.modules["assert_python_floors"] = assert_python_floors
_specification.loader.exec_module(assert_python_floors)


def make_distribution(root: Path, relative: str, name: str, version: str) -> Path:
    directory = root / relative / f"{name}-{version}.dist-info"
    directory.mkdir(parents=True)
    (directory / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    return directory


def test_version_parsing_orders_releases_against_a_floor():
    parse = assert_python_floors.parse_version
    assert parse("70.3.0") < parse("78.1.1")
    assert parse("84.0.0") > parse("78.1.1")
    assert parse("1.2.1") == parse("1.2.1")
    # A suffix must not make a release compare as lower than its own number.
    assert parse("78.1.1.post1") >= parse("78.1.1")
    assert parse("2.0.0rc1") > parse("1.9.9")


def test_name_normalization_follows_pep_503():
    assert assert_python_floors.normalize("Foo_Bar.Baz") == "foo-bar-baz"


def test_distribution_below_floor_is_reported_with_its_path(tmp_path, capsys):
    make_distribution(tmp_path, "usr/lib/python3.14/site-packages", "setuptools", "70.3.0")

    exit_code = assert_python_floors.main(["setuptools=78.1.1", "--root", str(tmp_path)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "setuptools 70.3.0 < 78.1.1" in output
    assert "usr/lib/python3.14/site-packages" in output


def test_distribution_at_or_above_floor_passes(tmp_path, capsys):
    make_distribution(tmp_path, "usr/lib/python3.14/site-packages", "setuptools", "84.0.0")
    make_distribution(tmp_path, "usr/lib/python3.14/site-packages", "msgpack", "1.2.1")

    exit_code = assert_python_floors.main(
        ["setuptools=78.1.1", "msgpack=1.2.1", "--root", str(tmp_path)]
    )

    assert exit_code == 0
    assert "All checked distributions meet their floor." in capsys.readouterr().out


def test_every_copy_is_checked_not_only_the_first(tmp_path, capsys):
    # The failure this assertion exists for: one tree upgraded, another not.
    make_distribution(tmp_path, "usr/lib/python3.14/site-packages", "setuptools", "84.0.0")
    make_distribution(tmp_path, "usr/local/lib/agent/venv/site-packages", "setuptools", "70.3.0")

    exit_code = assert_python_floors.main(["setuptools=78.1.1", "--root", str(tmp_path)])

    assert exit_code == 1
    assert "usr/local/lib/agent/venv" in capsys.readouterr().out


def test_excluded_subtree_is_not_checked(tmp_path, capsys):
    make_distribution(tmp_path, "upstream/venv/lib/site-packages", "msgpack", "1.1.2")

    exit_code = assert_python_floors.main(
        ["msgpack=1.2.1", "--root", str(tmp_path), "--exclude", str(tmp_path / "upstream")]
    )

    assert exit_code == 0
    assert "found ['none']" in capsys.readouterr().out


def test_distribution_without_a_floor_is_ignored(tmp_path):
    make_distribution(tmp_path, "usr/lib/python3.14/site-packages", "requests", "2.0.0")
    assert assert_python_floors.main(["setuptools=78.1.1", "--root", str(tmp_path)]) == 0


def test_egg_info_layout_is_recognized(tmp_path, capsys):
    directory = tmp_path / "usr/lib/python3.14/site-packages" / "setuptools-70.3.0.egg-info"
    directory.mkdir(parents=True)

    assert assert_python_floors.main(["setuptools=78.1.1", "--root", str(tmp_path)]) == 1
    assert "70.3.0 < 78.1.1" in capsys.readouterr().out


def test_malformed_floor_argument_is_rejected(tmp_path):
    for specification in ["setuptools", "=1.0", "setuptools="]:
        try:
            assert_python_floors.parse_floors([specification])
        except SystemExit as error:
            assert "invalid floor" in str(error)
        else:  # pragma: no cover - guards against a silently accepted typo
            raise AssertionError(f"{specification!r} should have been rejected")
