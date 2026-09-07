# Проба P1 (SPEC §15.5): ставятся ли зависимости готовыми колёсами под Windows + Python 3.12.
# --only-binary=:all: запрещает сборку из исходников: нет колеса — установка падает.
$ErrorActionPreference = 'Stop'
$probeDir = Join-Path $env:TEMP 'odata1c-probe-p1'
if (Test-Path $probeDir) { Remove-Item -Recurse -Force $probeDir }
New-Item -ItemType Directory -Path $probeDir | Out-Null
$venv = Join-Path $probeDir '.venv'
$py = Join-Path $venv 'Scripts\python.exe'

Write-Output '--- окружение ---'
uv --version
[System.Environment]::OSVersion.VersionString

Write-Output '--- чистое окружение на 3.12 ---'
uv venv --python 3.12 $venv

$deps = @('mcp', 'httpx', 'pydantic', 'pyyaml', 'lxml', 'snowballstemmer',
          'ahocorasick_rs', 'uvicorn', 'keyring')

Write-Output '--- установка только из колёс ---'
uv pip install --python $py --only-binary=:all: @deps

Write-Output '--- версии установленного ---'
uv pip list --python $py

Write-Output '--- импорт в том же окружении ---'
& $py (Join-Path $PSScriptRoot 'p1_imports.py')
