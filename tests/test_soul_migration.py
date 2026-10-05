import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "docker/hermes/migrate_soul.py"
SPEC = importlib.util.spec_from_file_location("migrate_soul", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("suffix", ["", "\n## My notes\nKeep these\n", "<!-- custom -->\nNotes\n"])
def test_preserves_surrounding_content(suffix):
    text = "Profile\n" + MODULE.LEGACY_BLOCK + suffix
    output = MODULE.transform(text)
    assert output == "Profile\n" + MODULE.CURRENT_BLOCK + suffix
    assert MODULE.transform(output) == output


def test_custom_legacy_is_not_rewritten(tmp_path):
    path = tmp_path / "SOUL.md"
    original = MODULE.LEGACY_BLOCK.replace("Telegram", "Custom", 1)
    path.write_text(original)
    with pytest.raises(ValueError, match="left unchanged"):
        MODULE.migrate(path)
    assert path.read_text() == original
    assert len(list(tmp_path.iterdir())) == 1


def test_backup_is_exact_and_migration_idempotent(tmp_path):
    path = tmp_path / "SOUL.md"
    original = (MODULE.LEGACY_BLOCK + "\nPersonal notes\n").encode()
    path.write_bytes(original)
    assert MODULE.migrate(path)
    backups = list(tmp_path.glob("SOUL.md.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not MODULE.migrate(path)
    assert len(list(tmp_path.glob("SOUL.md.backup-*"))) == 1


def test_existing_v2_and_missing_file(tmp_path):
    assert MODULE.transform(MODULE.CURRENT_BLOCK) == MODULE.CURRENT_BLOCK
    assert MODULE.transform(MODULE.LEGACY_BLOCK + MODULE.CURRENT_BLOCK) == MODULE.CURRENT_BLOCK
    assert not MODULE.migrate(tmp_path / "missing")


def test_new_file_content_is_retained():
    assert MODULE.transform("Personal rules\n").startswith("Personal rules\n")