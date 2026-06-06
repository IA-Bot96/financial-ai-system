# Dependency vulnerability scan (run in CI and before a deploy).
# Installs pip-audit on demand and audits the pinned requirements.
$ErrorActionPreference = "Stop"
$req = Join-Path $PSScriptRoot "..\requirements.txt"

python -c "import pip_audit" 2>$null
if (-not $?) {
    Write-Host "Installing pip-audit..."
    python -m pip install --quiet pip-audit
}

Write-Host "Auditing $req"
python -m pip_audit -r $req --strict
