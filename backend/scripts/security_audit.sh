#!/usr/bin/env bash
# Dependency vulnerability scan (run in CI and before a deploy).
# Installs pip-audit on demand and audits the pinned requirements.
set -euo pipefail
REQ="$(dirname "$0")/../requirements.txt"

python -c "import pip_audit" 2>/dev/null || {
    echo "Installing pip-audit..."
    python -m pip install --quiet pip-audit
}

echo "Auditing $REQ"
python -m pip_audit -r "$REQ" --strict
