"""Projects created under the old name (restforge) keep working after the rename."""
import os

from typer.testing import CliRunner

from portwright import compat
from portwright.cli import app
from portwright.security.hashing import generate_api_key, parse_key_id
from portwright.services import Project


def test_legacy_config_env_and_vault(tmp_path, monkeypatch):
    p = Project.init(tmp_path, "old")
    # turn it into a pre-rename project
    (tmp_path / "portwright.yaml").rename(tmp_path / "restforge.yaml")
    env = (tmp_path / ".env").read_text().replace("PORTWRIGHT_", "RESTFORGE_")
    (tmp_path / ".env").write_text(env)
    monkeypatch.delenv("PORTWRIGHT_MASTER_KEY", raising=False)
    monkeypatch.delenv("PORTWRIGHT_JWT_SECRET", raising=False)
    for line in env.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            monkeypatch.setenv(k, v)
    assert compat.config_in(tmp_path).name == "restforge.yaml"
    assert compat.getenv("PORTWRIGHT_MASTER_KEY") == os.environ["RESTFORGE_MASTER_KEY"]
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["cred", "add", "db", "-t", "database", "-f", "driver=sqlite",
                                 "-f", "database=x.db"])
    assert r.exit_code == 0, r.output
    old_vault = tmp_path / ".restforge" / "credentials.vault"
    (tmp_path / ".portwright").rename(tmp_path / ".restforge")
    assert compat.vault_path(tmp_path, compat.VAULT_PATH) == old_vault
    r = CliRunner().invoke(app, ["cred", "list"])
    assert r.exit_code == 0 and "db" in r.output, r.output
    r = CliRunner().invoke(app, ["validate"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "portwright.yaml").exists()       # saves go to the existing file
    del p


def test_api_key_prefixes_and_shim():
    kid, key = generate_api_key()
    assert key.startswith("pw_") and parse_key_id(key) == kid
    assert parse_key_id("rf_abcd1234_secret") == "abcd1234"
    import portwright.config
    import restforge.config
    assert restforge.config is portwright.config
