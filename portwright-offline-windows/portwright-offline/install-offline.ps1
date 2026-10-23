<#
  Portwright offline installer (no internet needed).
  Usage (PowerShell, from the folder that contains this script and .\wheelhouse):
      .\install-offline.ps1                              # new install to C:\apps\portwright with the Oracle driver
      .\install-offline.ps1 -Target D:\portwright -Extras "oracle,mssql"
      .\install-offline.ps1 -Upgrade                     # update an existing install (also a restforge one)
  Extras: oracle, postgres, mysql, mssql, keyring  (comma-separated, or "" for none)
  Requires Python 3.12 or 3.13 (64-bit) already installed (offline installer from python.org).

  Upgrading from restforge: run with -Upgrade. An install in C:\apps\restforge is found automatically
  and stays where it is; the old restforge package is replaced by portwright. The commands
  portwright, pw and restforge all work afterwards, and your projects are not touched.
#>
param(
  [string]$Target = "",
  [string]$Extras = "oracle",
  [string]$Python = "python",
  [switch]$Upgrade
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$wheels = Join-Path $here "wheelhouse"
$pkg = if ($Extras) { "portwright[$Extras]" } else { "portwright" }

if (-not $Target) {
  $Target = "C:\apps\portwright"
  if (-not (Test-Path "$Target\.venv\Scripts\python.exe") -and (Test-Path "C:\apps\restforge\.venv\Scripts\python.exe")) {
    $Target = "C:\apps\restforge"         # existing install from before the rename
  }
}
$py = Join-Path $Target ".venv\Scripts\python.exe"

if ($Upgrade -or (Test-Path $py)) {
  if (-not (Test-Path $py)) { throw "No install found in $Target - run without -Upgrade first." }
  Write-Host "Updating the install in $Target (stop the server, service and Studio first)..."
  & $py -m pip show restforge 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Host "Replacing the old restforge package with portwright..."
    & $py -m pip uninstall -y restforge
    if ($LASTEXITCODE -ne 0) { throw "could not remove restforge - is it still running? (files in use)" }
  }
  & $py -m pip install --no-index --find-links $wheels --upgrade --force-reinstall --no-deps portwright
  if ($LASTEXITCODE -ne 0) { throw "update failed - is Portwright still running? (files in use)" }
  & $py -m pip install --no-index --find-links $wheels $pkg | Out-Null   # any new dependency (also pins fastapi < 0.142)
  & $py -m pip show opentelemetry-api 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing opentelemetry-api (no longer needed)..."
    & $py -m pip uninstall -y opentelemetry-api | Out-Null
  }
  & (Join-Path $Target ".venv\Scripts\portwright.exe") --version
  Write-Host "Updated. Commands: portwright, pw (alias), restforge (old name). Projects were not touched."
  exit 0
}

$ver = & $Python -c "import sys, struct; print(f'{sys.version_info[0]}.{sys.version_info[1]} {struct.calcsize(""P"")*8}')"
Write-Host "Found Python $ver"
if ($ver -notmatch '^3\.1[23] 64$') { throw "Python 3.12 or 3.13 (64-bit) is required; this bundle has wheels only for those." }

New-Item -ItemType Directory -Force -Path $Target | Out-Null
& $Python -m venv (Join-Path $Target ".venv")
& $py -m pip install --no-index --find-links $wheels --upgrade pip
& $py -m pip install --no-index --find-links $wheels $pkg
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& (Join-Path $Target ".venv\Scripts\portwright.exe") --version
Write-Host ""
Write-Host "Installed. Activate with:  $Target\.venv\Scripts\Activate.ps1"
Write-Host "Then:  portwright init C:\apps\my-api --name my-api     (or the short alias: pw init ...)"
