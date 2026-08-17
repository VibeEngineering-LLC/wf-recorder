import os
import wf_pull_client as wpc
from test_data7_name_reuse import _make_seg, CH


def test_append_rolls_back_on_write_failure(tmp_path, monkeypatch):
    """#DATA-8 место 2: обрыв дозаписи откатывается (truncate), не разъезжает
    границы строк. Повтор после отката обязан дописать строку ЦЕЛИКОМ, без
    дублей и без хвоста от сбойной попытки."""
    st = wpc.Stitcher(str(tmp_path / "shov.aswf"))
    st.append_segment("seg_00001.aswf", _make_seg([[10] * CH], seg_seq=1))
    pre_size = os.path.getsize(st.path)

    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk failure mid-write")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky_fsync)
    blob2 = _make_seg([[20] * CH], seg_seq=2)
    try:
        st.append_segment("seg_00002.aswf", blob2)
        assert False, "ожидался OSError от сбойного fsync"
    except OSError:
        pass

    assert os.path.getsize(st.path) == pre_size, \
        "файл не откатился к размеру ДО сбойного сегмента — граница строки разъедена"

    monkeypatch.undo()
    rows, gap, diag = st.append_segment("seg_00002.aswf", blob2)
    assert rows == 1, "повтор после отката обязан дописать строку целиком"

    with open(st.path, "rb") as f:
        raw = f.read()
    hdr, prefix, payload = wpc.parse_aswf(raw, "shov.aswf")
    stride = hdr.get("row_stride", hdr["channels"] * 2)
    assert len(payload) % stride == 0, "после отката+повтора границы строк разъехались"
    assert len(payload) // stride == 2, "ожидалось ровно 2 строки (1 до сбоя + 1 после отката)"
