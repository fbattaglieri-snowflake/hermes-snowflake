#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

python -m py_compile \
  proxy/cortex_proxy.py \
  tooling/cortex_model_gate.py \
  tooling/refresh_cortex_models.py \
  tooling/cortex_wire_check.py \
  docker/hermes/hermes_configure.py
ruff check proxy tooling docker/hermes
pytest proxy -q
python -m json.tool proxy/models.json >/dev/null
yamllint -d '{extends: default, rules: {line-length: disable, document-start: disable, truthy: disable, empty-lines: disable, braces: disable}}' \
  .github infrastructure/specs

if grep -RIE --exclude-dir=.git --exclude-dir=.venv \
  '(github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----)' .; then
  echo "Potential secret material detected" >&2
  exit 1
fi

echo "Validation passed"
