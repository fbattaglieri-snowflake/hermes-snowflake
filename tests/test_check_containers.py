import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_containers", Path(__file__).parents[1] / "scripts/check_containers.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("rows,expected", [
    ([], False), ({}, False), ([{}], False), ([None], False),
    ([{"status": "READY"}], True), ([{"STATUS": "READY"}], True),
    ([{"status": "READY"}, {"status": "PENDING"}], False),
    ([{"status": "FAILED"}], False), ([{"status": "RUNNING"}], False),
])
def test_readiness_requires_every_container(rows, expected):
    assert MODULE.ready(rows) is expected