import sys
import os
import time
import threading
import pytest

# Добавляем путь к модулям wf_recorder_app и wf_pull_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wf_recorder_app
import wf_pull_client as wpc

sys.stdout.reconfigure(encoding="utf-8")


def test_stop_join_gives_thread_really_dead(monkeypatch, tmp_path):
    # Мокаем функции из wf_pull_client
    monkeypatch.setattr(wf_recorder_app.wpc, "get_csrf", lambda host: "tok")
    monkeypatch.setattr(wf_recorder_app.wpc, "list_segments", lambda host: time.sleep(0.05) or [])
    monkeypatch.setattr(wf_recorder_app.wpc, "http_get", lambda url, headers=None, binary=False: "{}")
    monkeypatch.setattr(wpc.Stitcher, "log_temps", lambda self, host: None)

    core = wf_recorder_app.RecorderCore("http://fake-host", str(tmp_path / "seam.aswf"), interval=100)
    core.start()

    # Ждем старта потока
    max_attempts = 50
    attempts = 0
    while not core._thread.is_alive() and attempts < max_attempts:
        time.sleep(0.01)
        attempts += 1
    if attempts >= max_attempts:
        pytest.fail("поток не стартовал")

    ok = core.stop(timeout=2.0)
    assert ok is True, "stop() должен вернуть True, когда поток гарантированно остановлен"
    assert not core._thread.is_alive(), "поток должен быть мёртв сразу после успешного stop()"


def test_stop_timeout_not_blocked_by_interval(monkeypatch, tmp_path):
    # Мокаем функции из wf_pull_client
    monkeypatch.setattr(wf_recorder_app.wpc, "get_csrf", lambda host: "tok")
    monkeypatch.setattr(wf_recorder_app.wpc, "list_segments", lambda host: time.sleep(0.05) or [])
    monkeypatch.setattr(wf_recorder_app.wpc, "http_get", lambda url, headers=None, binary=False: "{}")
    monkeypatch.setattr(wpc.Stitcher, "log_temps", lambda self, host: None)

    core = wf_recorder_app.RecorderCore("http://fake-host", str(tmp_path / "seam.aswf"), interval=100)
    core.start()

    # Ждем старта потока
    max_attempts = 50
    attempts = 0
    while not core._thread.is_alive() and attempts < max_attempts:
        time.sleep(0.01)
        attempts += 1
    if attempts >= max_attempts:
        pytest.fail("поток не стартовал")

    start_time = time.monotonic()
    ok = core.stop(timeout=2.0)
    end_time = time.monotonic()

    assert ok is True, "stop() должен вернуть True"
    assert end_time - start_time < 3.0, "stop() должен завершиться быстро, не дожидаясь interval"

    assert not core._thread.is_alive(), "поток должен быть мёртв сразу после успешного stop()"


def test_second_core_blocked_while_first_alive_or_both_dead(monkeypatch, tmp_path):
    path = str(tmp_path / "seam2.aswf")

    # Мокаем функции из wf_pull_client
    monkeypatch.setattr(wf_recorder_app.wpc, "get_csrf", lambda host: "tok")
    monkeypatch.setattr(wf_recorder_app.wpc, "list_segments", lambda host: time.sleep(0.05) or [])
    monkeypatch.setattr(wf_recorder_app.wpc, "http_get", lambda url, headers=None, binary=False: "{}")
    monkeypatch.setattr(wpc.Stitcher, "log_temps", lambda self, host: None)

    core1 = wf_recorder_app.RecorderCore("http://fake-host", path, interval=100)
    core1.start()

    # Ждем старта потока
    max_attempts = 50
    attempts = 0
    while not core1._thread.is_alive() and attempts < max_attempts:
        time.sleep(0.01)
        attempts += 1
    if attempts >= max_attempts:
        pytest.fail("поток не стартовал")

    ok = core1.stop(timeout=2.0)
    assert ok

    # После успешного stop() поток core1 гарантированно мёртв — второй core можно спокойно создавать
    core2 = wf_recorder_app.RecorderCore("http://fake-host", path, interval=100)
    core2.start()

    # Ждем старта второго потока
    attempts = 0
    while not core2._thread.is_alive() and attempts < max_attempts:
        time.sleep(0.01)
        attempts += 1
    if attempts >= max_attempts:
        pytest.fail("второй поток не стартовал")

    assert not core1._thread.is_alive()
    assert core2._thread.is_alive()

    ok = core2.stop(timeout=2.0)
    assert ok is True
