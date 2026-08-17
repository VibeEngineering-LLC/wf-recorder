import wf_pull_client as wpc


def test_filemode_rejects_non_aswf_content(tmp_path, monkeypatch):
    """#DATA-8 место 4: совпадение размера с HTML-страницей ошибки не должно
    уходить в ack — файловый режим обязан проверить магию/шапку ASWF."""
    fake_html = b"<html>502 Bad Gateway from proxy</html>"
    ack_calls = []
    monkeypatch.setattr(wpc, "http_get", lambda *a, **k: fake_html)
    monkeypatch.setattr(wpc, "ack_delete", lambda *a, **k: (ack_calls.append(1), True)[1])

    seg = {"name": "seg_0001.aswf", "bytes": len(fake_html)}
    result = wpc.fetch_one_filemode("http://board", str(tmp_path), seg, "tok")

    assert result.startswith("error:badcontent:"), \
        f"ожидался статус error:badcontent:, получено {result}"
    assert not ack_calls, "ack_delete вызван на невалидном содержимом — плата очищена"
    assert not (tmp_path / "seg_0001.aswf").exists(), \
        "невалидный контент не должен публиковаться как файл сегмента"
