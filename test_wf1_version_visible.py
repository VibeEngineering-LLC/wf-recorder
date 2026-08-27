# -*- coding: utf-8 -*-
"""#WF-1: версия видна и привязана к _version.py (мутация 9.9.9 → тест краснеет)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _expected():
    sys.path.insert(0, HERE)
    from _version import __version__ as v
    return v


def test_version_flag_prints_version():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    out = subprocess.check_output(
        [sys.executable, "wf_recorder_app.py", "--version"],
        cwd=HERE, stderr=subprocess.STDOUT, env=env).decode()
    assert "wf-recorder" in out and _expected() in out


def test_version_from_source_not_fallback():
    sys.path.insert(0, HERE)
    import wf_recorder_app as app
    assert app.VERSION == _expected() != "0.0.0"
    assert app.VERSION_LINE == "wf-recorder v" + _expected()
