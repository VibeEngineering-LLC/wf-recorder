import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
import wf_pull_client as wpc


def test_ingest_confirmed_survives_alien_schema(tmp_path):
    """#DATA-9: state.json чужой схемы (без ключа 'ingested') не должен ронять
    ingest_confirmed() -- раньше падал KeyError на self.state["ingested"]."""
    seam = tmp_path / "seam.aswf"
    state_path = tmp_path / "seam.aswf.state.json"
    seam.write_bytes(b"ASWFxxxxxxxxxxxxxxxxxxxxxxxxxx")  # >=8 Б, file_ok=True
    state_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    stitcher = wpc.Stitcher(str(seam))
    result = stitcher.ingest_confirmed("seg_00001.aswf", "somehash")
    assert result is False
