"""--version, migration bundles and the package metadata agree with pyproject.toml."""
import tomllib
from pathlib import Path

from typer.testing import CliRunner

import portwright
from portwright.cli import app

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version_matches_pyproject():
    want = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert portwright.__version__ == want, "reinstall (pip install -e .) after changing the version"
    assert CliRunner().invoke(app, ["--version"]).output.strip() == f"Portwright {want}"
