import sys, os, io, contextlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
import wf_pull_client as wpc


def test_sizemismatch_escalates_after_repeats(tmp_path, monkeypatch):
    """#DATA-9: повторяющийся sizemismatch не должен молчать бесконечно."""
    stitcher = wpc.Stitcher(str(tmp_path / "seam.aswf"))
    seg = {"name": "seg_00099.aswf", "bytes": 1000, "idx": 99, "finalized": True}
    monkeypatch.setattr(wpc, "http_get", lambda url, **kw: b"wrong-size-blob")

    last_output = ""
    for _ in range(5):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status, *_ = wpc.fetch_one_stitch("http://fake", stitcher, seg, "tok")
        last_output = buf.getvalue()
        assert status == "sizemismatch"

    assert "#DATA-9" in last_output, f"эскалации нет: {last_output!r}"
