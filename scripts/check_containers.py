import json
import sys


def ready(rows):
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        normalized = {key.lower(): value for key, value in row.items()}
        if str(normalized.get("status", "")).upper() != "READY":
            return False
    return True


if __name__ == "__main__":
    sys.exit(0 if ready(json.load(sys.stdin)) else 1)