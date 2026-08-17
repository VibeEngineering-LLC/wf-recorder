import os, json, struct
import wf_pull_client as wpc
from test_data7_name_reuse import _make_seg


def _read_calibration(path):
    with open(path, "rb") as f:
        f.read(4)
        hlen = struct.unpack("<I", f.read(4))[0]
        return json.loads(f.read(hlen).decode())["calibration"]


def test_calibration_change_rotates_file(tmp_path):
    """#DATA-8: смена калибровки должна ротировать файл шва, не молча вшивать
    строки под чужой калибровкой (как уже делает смена формата, #REC-14)."""
    st = wpc.Stitcher(str(tmp_path / "shov.aswf"))
    blob_a = _make_seg([[10] * 8192], seg_seq=1, calibration=[0.0, 3.0, 0.0])
    st.append_segment("seg_00001.aswf", blob_a)
    blob_b = _make_seg([[20] * 8192], seg_seq=2, calibration=[1.0, 3.5, 0.0])
    st.append_segment("seg_00002.aswf", blob_b)

    files = sorted(f for f in os.listdir(tmp_path) if f.endswith(".aswf"))
    assert len(files) == 2, f"ожидалась ротация на 2 файла, получено: {files}"
    cals = [_read_calibration(tmp_path / f) for f in files]
    assert [0.0, 3.0, 0.0] in cals and [1.0, 3.5, 0.0] in cals
