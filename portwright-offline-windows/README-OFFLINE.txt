Portwright - offline installation bundle (Windows x64, Python 3.12 / 3.13)
=========================================================================
Portwright is the new name of restforge. Commands: portwright, the short alias pw, and the old
name restforge (kept so existing scripts, Stonebranch tasks and NSSM services keep working).

New in 0.6.0 - activity log and migration to another instance
  Every setup change (Studio, CLI, hand edits of portwright.yaml) is logged with user, time and the
  equivalent pw command: Studio > Activity, or pw activity. Move a setup to test/prod with a replay
  script (pw migrate script) or a bundle (pw migrate export / plan / import). Guide: pw docs migration

New in 0.5.x - Active Directory group loads
  A load can fill an AD group (kill & fill, sync or add only) from e-mail addresses returned by a
  query endpoint or a file. Add a source of type ldap (Sources > Add source, or
  pw source add corp_ad -t ldap --server ldaps://dc01.corp.local --base-dn "DC=corp,DC=local" --credential ad_svc),
  then a load with "Write to: AD group". Guide: pw docs ad-groups
  Needs: TCP 636 (LDAPS) from this server to a domain controller, and a service account with
  "Write Members" on the group - or the RSAT ActiveDirectory PowerShell module (directory: powershell).
  The new packages (ldap3, pyasn1) are in the wheelhouse; -Upgrade installs them.
  Set a test group on the source (pw source set-test-group corp_ad HR-Staff) and pw source test
  corp_ad lists its members - a quick check of the connection and permissions.
  Always run the first load with --dry-run (or Preview in Studio) and read the remove list.

Contents
  wheelhouse\            portwright + every dependency as .whl files (no internet needed)
  install-offline.ps1    one-command installer (new install or upgrade)
  portwright-source.zip  source code, docs (README.md, ARCHITECTURE.md) and tests

Upgrading an existing restforge install (keeps your projects)
  Stop the server / NSSM service / Studio, copy this folder to the server, then:
        cd C:\install\portwright-offline
        Set-ExecutionPolicy -Scope Process Bypass
        .\install-offline.ps1 -Upgrade
  C:\apps\restforge is found automatically and stays where it is (add -Target <folder> otherwise).
  Nothing in your projects has to change: restforge.yaml, RESTFORGE_* lines in .env, the .restforge
  vault folder and rf_ API keys are all still recognised.

New install on an offline server
  1. Install Python 3.12 or 3.13 64-bit with the python.org *offline* installer
     (python-3.12.x-amd64.exe; tick "Install for all users" and "Add to PATH").
  2. Copy this whole folder to the server (USB / file share), e.g. C:\install\portwright-offline
  3. PowerShell:
        cd C:\install\portwright-offline
        Set-ExecutionPolicy -Scope Process Bypass
        .\install-offline.ps1 -Target C:\apps\portwright -Extras "oracle"
  4. C:\apps\portwright\.venv\Scripts\Activate.ps1
     pw init C:\apps\hr-api --name hr-api
     pw docs              (the user guide; also the Help screen in Studio)

Manual install (instead of step 3)
     python -m venv C:\apps\portwright\.venv
     C:\apps\portwright\.venv\Scripts\python -m pip install --no-index --find-links .\wheelhouse "portwright[oracle]"

Other things to copy if you need them (also offline-capable)
  - SQL Server: Microsoft "ODBC Driver 18 for SQL Server" MSI (Oracle/Postgres/MySQL need nothing extra)
  - NSSM (nssm.exe) to run Portwright as a Windows service
  - Your TLS certificate as PEM files, or IIS + ARR as an HTTPS front end

At runtime Portwright makes no internet calls. The API docs page (/api/docs) is served
from files inside the package. Only 'rest' sources and 'oauth2' credentials make outbound
calls, and only to the hosts you allow-list (use internal hosts on an offline network).
