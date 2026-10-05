import os
import shutil
import sys
import tempfile
from pathlib import Path

LEGACY_BLOCK = '''<!-- spcs-telegram-v1 -->
## Invio messaggi su Telegram

Non esiste un tool di invio messaggi richiamabile dal modello.
Per inviare su Telegram usa il tool `terminal`:

    hermes send --to telegram "testo del messaggio"

Il destinatario predefinito e` TELEGRAM_HOME_CHANNEL, gia`
configurato: non chiedere il chat_id se non te lo danno.
Per una chat diversa: `--to telegram:<chat_id>`.
Per elencare i target disponibili: `hermes send --list telegram`.
Non usare computer_use per Telegram: il container e` headless.
'''

CURRENT_BLOCK = '''<!-- spcs-telegram-v2 -->
## Sending messages on Telegram

There is no message-sending tool callable by the model.
To send on Telegram, use the `terminal` tool:

    hermes send --to telegram "message text"

The default recipient is TELEGRAM_HOME_CHANNEL, already
configured: do not ask for the chat_id unless given one.
For a different chat: `--to telegram:<chat_id>`.
To list the available targets: `hermes send --list telegram`.
Do not use computer_use for Telegram: the container is headless.
<!-- /spcs-telegram-v2 -->
'''


def transform(text):
    legacy_marker = "<!-- spcs-telegram-v1 -->"
    current_marker = "<!-- spcs-telegram-v2 -->"
    if legacy_marker in text:
        if text.count(legacy_marker) != 1 or LEGACY_BLOCK not in text:
            raise ValueError("Customized legacy Telegram block: SOUL.md left unchanged")
        replacement = "" if current_marker in text else CURRENT_BLOCK
        return text.replace(LEGACY_BLOCK, replacement, 1)
    if current_marker in text:
        return text
    return text + "\n" + CURRENT_BLOCK


def migrate(path):
    path = Path(path)
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError("SOUL.md must be a regular file")
    original = path.read_bytes()
    updated = transform(original.decode("utf-8")).encode("utf-8")
    if updated == original:
        return False
    backup_fd, backup_name = tempfile.mkstemp(prefix="SOUL.md.backup-", dir=path.parent)
    with os.fdopen(backup_fd, "wb") as backup:
        backup.write(original)
    temporary_fd, temporary_name = tempfile.mkstemp(prefix=".SOUL-", dir=path.parent)
    try:
        with os.fdopen(temporary_fd, "wb") as temporary:
            temporary.write(updated)
        shutil.copymode(path, temporary_name)
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    print(f"[hermes] SOUL.md updated; backup: {Path(backup_name).name}")
    return True


if __name__ == "__main__":
    try:
        migrate(sys.argv[1])
    except (ValueError, OSError) as error:
        print(f"[hermes] WARN: {error}", file=sys.stderr)
        sys.exit(1)