import wf_pull_client as wpc
from test_data7_name_reuse import _make_seg, CH


class _FakeStitcher:
    def ingest_confirmed(self, name, digest):
        return False

    def clear_sizemismatch(self, name):
        pass

    def append_segment(self, name, blob):
        return 1, None, {"crc_bad": 1, "crc_checked": 1, "seq_gap": 0, "recon": None}


def test_crc_bad_withholds_ack(monkeypatch):
    """#DATA-8 место 3: crc_bad>0 обязан удержать ack — иначе стирается
    последняя целая копия на плате, а битая остаётся единственной на диске."""
    blob = _make_seg([[10] * CH])
    ack_calls = []
    monkeypatch.setattr(wpc, "http_get", lambda *a, **k: blob)
    monkeypatch.setattr(wpc, "ack_delete", lambda *a, **k: (ack_calls.append(1), True)[1])

    seg = {"name": "seg_0001.aswf", "bytes": len(blob)}
    status, rows, gap, diag = wpc.fetch_one_stitch("http://board", _FakeStitcher(), seg, "tok")

    assert status == "held:crc_bad", f"ожидался статус held:crc_bad, получено {status}"
    assert not ack_calls, "ack_delete вызван при crc_bad>0 — хорошая копия на плате стёрта"
