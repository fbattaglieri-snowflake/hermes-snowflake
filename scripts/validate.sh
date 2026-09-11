#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

python3 -m py_compile \
  proxy/cortex_proxy.py \
  tooling/cortex_model_gate.py \
  tooling/refresh_cortex_models.py \
  tooling/cortex_wire_check.py \
  docker/hermes/hermes_configure.py \
  docker/hermes/migrate_soul.py
python3 -m ruff check proxy tooling docker/hermes scripts tests
python3 -m pytest proxy tests -q
python3 -m json.tool proxy/models.json >/dev/null
bash -n scripts/wait_for_services.sh docker/hermes/start.sh docker/hermes/spcs_token.sh
yamllint -d '{extends: default, rules: {line-length: disable, document-start: disable, truthy: disable, empty-lines: disable, braces: disable}}' \
  .github infrastructure/specs

if grep -RIE --exclude-dir=.git --exclude-dir=.venv \
  '(github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----)' .; then
  echo "Potential secret material detected" >&2
  exit 1
fi

echo "Validation passed"
