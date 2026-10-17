import json
import os
import re
import sys

import pytest

from conftest import http

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import launcher  # noqa: E402
import version  # noqa: E402


def test_version_strings():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)
    assert isinstance(version.API_VERSION, int) and version.API_VERSION >= 1


def test_prepare_data_dir_seeds_config_and_never_overwrites(tmp_path):
    home = tmp_path / "ls-home"
    launcher.prepare_data_dir(str(home))
    seeded = json.loads((home / "config.json").read_text())
    assert "models" in seeded
    (home / "config.json").write_text('{"mine": true}')
    launcher.prepare_data_dir(str(home))
    assert json.loads((home / "config.json").read_text()) == {"mine": True}


def test_data_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLAMASHIFT_HOME", str(tmp_path / "x"))
    assert launcher.data_dir() == str(tmp_path / "x")


def test_version_flag_prints_and_returns(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["llamashift", "--version"])
    launcher.run()
    assert capsys.readouterr().out.strip() == f"llamashift {version.__version__} (api {version.API_VERSION})"


@pytest.mark.parametrize("path", ["/api/version", "/api/health"])
def test_version_is_served_without_auth(start_server, path):
    srv = start_server()
    status, body = http("GET", f"http://127.0.0.1:{srv.port}{path}")[:2]
    data = body if isinstance(body, dict) else json.loads(body)
    assert status == 200
    assert data["version"] == version.__version__ and data["apiVersion"] == version.API_VERSION
