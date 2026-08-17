import os
import wf_pull_client as wpc
from test_data7_name_reuse import _make_seg, CH


def test_orphan_tail_truncated_before_append(tmp_path):
    """#DATA-8 место 2, находка Codeaudit P2: огрызок от kill -9 / потери питания
    (truncate выполнить некому) обязан срезаться на входе следующего процесса.
    Иначе он остаётся в СЕРЕДИНЕ файла и делает нечитаемым всё, что после него —
    финальный разбор .aswf отбрасывает только хвост."""
    seam = tmp_path / "shov.aswf"
    st = wpc.Stitcher(str(seam))
    st.append_segment("seg_00001.aswf", _make_seg([[10] * CH], seg_seq=1))
    good_size = os.path.getsize(seam)

    with open(seam, "ab") as f:      # эмуляция kill -9 посреди write
        f.write(b"\x00" * 777)
    assert os.path.getsize(seam) == good_size + 777

    st2 = wpc.Stitcher(str(seam))    # новый процесс поднимается на битом файле
    st2.append_segment("seg_00002.aswf", _make_seg([[20] * CH], seg_seq=2))

    with open(seam, "rb") as f:
        hdr, prefix, payload = wpc.parse_aswf(f.read(), "shov.aswf")
    stride = hdr.get("row_stride", hdr["channels"] * 2)
    assert len(payload) % stride == 0, \
        "огрызок остался в середине файла — все строки после него нечитаемы"
    assert len(payload) // stride == 2, "ожидались ровно 2 целые строки"
