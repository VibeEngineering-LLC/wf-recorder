# -*- coding: utf-8 -*-
"""
Тесты для проверки обработки TimeoutError в wf_pull_client.

Баг #1 от BOPOHOP (2026-08-13): если плата перезагружается ровно во время сетевого запроса,
urllib бросает TimeoutError. TimeoutError — подкласс OSError, но НЕ подкласс
urllib.error.URLError. Код ловит только (urllib.error.URLError, urllib.error.HTTPError)
в пяти местах — TimeoutError пролетает мимо, процесс падает трейсбеком.

Эти тесты проверяют, что TimeoutError теперь корректно обрабатывается и не приводит к падению
в непрерывном режиме.
"""

import sys
import urllib.error
import urllib.request
import pytest
import wf_pull_client as wpc


def test_timeout_is_not_urlerror():
    """Проверяет иерархию исключений: TimeoutError подкласс OSError, но не URLError."""
    assert issubclass(TimeoutError, OSError)
    assert not issubclass(TimeoutError, urllib.error.URLError)


@pytest.mark.parametrize("call", [
    lambda: wpc.http_get("http://x", None, True),
    lambda: wpc.ack_delete("http://x", "seg1", "tok"),
])
def test_timeout_propagates_from_network_layer(monkeypatch, call):
    """Проверяет, что TimeoutError пролетает мимо обработки в сетевом слое."""
    def mock_urlopen(*args, **kwargs):
        raise TimeoutError("simulated reboot mid-request")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    with pytest.raises(TimeoutError):
        call()


def test_fetch_one_filemode_survives_delete_timeout(monkeypatch, tmp_path):
    """Проверяет, что fetch_one_filemode выживает при TimeoutError в ack_delete."""
    seg = {"name": "seg_0001.aswf", "bytes": 4}
    file_path = tmp_path / seg["name"]
    file_path.write_bytes(b"data")

    def mock_ack_delete(*args, **kwargs):
        raise TimeoutError("board rebooted before responding to delete")

    # #DATA-7: скачивание больше не пропускается по совпадению (имя, размер),
    # поэтому до ack_delete тест доходит только с рабочим http_get
    monkeypatch.setattr(wpc, "http_get", lambda *a, **k: b"data")
    monkeypatch.setattr(wpc, "ack_delete", mock_ack_delete)
    result = wpc.fetch_one_filemode("http://board", str(tmp_path), seg, "tok")
    assert result.startswith("error:del:"), f"Ожидался статус с 'error:del:', но получено: {result}"


def test_fetch_one_stitch_survives_delete_timeout(monkeypatch):
    """Проверяет, что fetch_one_stitch выживает при TimeoutError в ack_delete."""
    class FakeStitcher:
        def ingest_confirmed(self, name, digest):
            return True          # #DATA-7: подтверждённый дубль — строки уже в шве

    seg = {"name": "seg_0002.aswf", "bytes": 4}

    def mock_ack_delete(*args, **kwargs):
        raise TimeoutError("board rebooted before responding to delete")

    monkeypatch.setattr(wpc, "http_get", lambda *a, **k: b"data")
    monkeypatch.setattr(wpc, "ack_delete", mock_ack_delete)
    status, rows, gap, diag = wpc.fetch_one_stitch("http://board", FakeStitcher(), seg, "tok")
    assert status.startswith("error:del:"), f"Ожидался статус с 'error:del:', но получено: {status}"


def test_fetch_one_filemode_survives_get_timeout(monkeypatch, tmp_path):
    """Проверяет, что fetch_one_filemode выживает при TimeoutError в http_get."""
    seg = {"name": "seg_0003.aswf", "bytes": 4}

    def mock_http_get(*args, **kwargs):
        raise TimeoutError("board rebooted mid-download")

    monkeypatch.setattr(wpc, "http_get", mock_http_get)
    result = wpc.fetch_one_filemode("http://board", str(tmp_path), seg, "tok")
    assert result.startswith("error:get:"), f"Ожидался статус с 'error:get:', но получено: {result}"


def test_fetch_one_stitch_survives_get_timeout(monkeypatch):
    """Проверяет, что fetch_one_stitch выживает при TimeoutError в http_get."""
    class FakeStitcher:
        def ingest_confirmed(self, name, digest):
            return False

    seg = {"name": "seg_0004.aswf", "bytes": 4}

    def mock_http_get(*args, **kwargs):
        raise TimeoutError("board rebooted mid-download")

    monkeypatch.setattr(wpc, "http_get", mock_http_get)
    status, rows, gap, diag = wpc.fetch_one_stitch("http://board", FakeStitcher(), seg, "tok")
    assert status.startswith("error:get:"), f"Ожидался статус с 'error:get:', но получено: {status}"


def test_main_loop_survives_timeout_and_continues(monkeypatch, capsys):
    """Проверяет, что основной цикл не падает при TimeoutError и продолжает работу."""
    calls = {"n": 0}

    def mock_one_pass(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("board rebooted mid-pass")
        elif calls["n"] == 2:
            raise SystemExit(0)

    monkeypatch.setattr(wpc, "one_pass", mock_one_pass)
    monkeypatch.setattr(wpc.time, "sleep", lambda x: None)
    sys.argv = ["wf_pull_client.py", "--interval", "1"]

    with pytest.raises(SystemExit):
        wpc.main()

    assert calls["n"] == 2, f"Ожидалось 2 вызова one_pass, но получено: {calls['n']}"

    output = capsys.readouterr().out
    assert ("TimeoutError" in output or "board rebooted" in output or "проход не удался" in output), \
        f"Вывод должен содержать информацию об ошибке, но был: {output}"