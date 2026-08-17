import wf_pull_client as wpc
from test_data7_name_reuse import _make_seg, CH


class _TwoPassStitcher:
    """Воспроизводит РЕАЛЬНОЕ поведение Stitcher между проходами: append_segment
    регистрирует отпечаток безусловно, поэтому на 2-м проходе ingest_confirmed
    отвечает True и diag становится None (находка Codeaudit P1, 2026-08-17)."""

    def __init__(self):
        self.seen = False
        self.flagged = set()

    def ingest_confirmed(self, name, digest):
        return self.seen

    def ingest_crc_bad(self, name):
        return name in self.flagged

    def clear_sizemismatch(self, name):
        pass

    def append_segment(self, name, blob):
        self.seen = True
        self.flagged.add(name)
        return 1, None, {"crc_bad": 1, "crc_checked": 1, "seq_gap": 0, "recon": None}


def test_crc_bad_holds_ack_across_passes(monkeypatch):
    """#DATA-8 место 3, находка Codeaudit P1: удержание ack обязано пережить
    проход. Раньше на 2-м проходе ingest_confirmed=True обнулял diag, held-ветка
    пропускалась и битый сегмент удалялся — исходный дефект с задержкой."""
    blob = _make_seg([[10] * CH])
    ack_calls = []
    monkeypatch.setattr(wpc, "http_get", lambda *a, **k: blob)
    monkeypatch.setattr(wpc, "ack_delete", lambda *a, **k: (ack_calls.append(1), True)[1])

    seg = {"name": "seg_0001.aswf", "bytes": len(blob)}
    st = _TwoPassStitcher()

    s1 = wpc.fetch_one_stitch("http://board", st, seg, "tok")[0]
    assert s1 == "held:crc_bad", f"проход 1: ожидался held:crc_bad, получено {s1}"

    s2 = wpc.fetch_one_stitch("http://board", st, seg, "tok")[0]
    assert s2 == "held:crc_bad", f"проход 2: ожидался held:crc_bad, получено {s2}"
    assert not ack_calls, \
        "ack ушёл на повторном проходе — битый сегмент стёрт, целая копия потеряна"
