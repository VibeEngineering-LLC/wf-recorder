import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
import wf_pull_client as wpc

HOST = "http://atomspectra.local"
STITCH_PATH = (
    "C:/Users/1/AppData/Local/Temp/claude/"
    "D--GoogleDrive----------------------------/"
    "c710f0ac-1e9a-4e14-8a07-dfca55c2607e/scratchpad/A1/seam_A1.aswf"
)

def main():
    # Подмена функции ack_delete
    _orig_ack_delete = wpc.ack_delete
    _patch_flag = {"armed": True}

    def patched_ack_delete(host, name, token):
        if _patch_flag["armed"]:
            _patch_flag["armed"] = False
            raise TimeoutError("C3-тест: искусственный обрыв связи перед ack_delete")
        return _orig_ack_delete(host, name, token)

    stitcher = wpc.Stitcher(STITCH_PATH)
    token = wpc.get_csrf(HOST)
    segments = wpc.list_segments(HOST)
    pending = [s for s in segments if s.get("finalized") is True]
    pending.sort(key=lambda s: int(s["idx"]))

    if not pending:
        print("Не хватает данных для теста C3")
        return 2

    seg = pending[0]
    print(f"Сегмент: {seg['name']}, размер: {seg['bytes']}, строки: {seg.get('rows')}")
    rows_before = stitcher.state.get("rows", 0)

    # Проход 1 — с порчей
    wpc.ack_delete = patched_ack_delete
    try:
        status, rows, gap, diag = wpc.fetch_one_stitch(HOST, stitcher, seg, token)
    finally:
        wpc.ack_delete = _orig_ack_delete

    print(f"Проход 1: status={status}, rows={rows}")
    assert status.startswith("error:del"), f"Ожидается статус с 'error:del', но получено '{status}'"
    assert rows == seg.get("rows"), "Количество строк не совпадает с заявленным"
    
    rows_added = stitcher.state.get("rows", 0) - rows_before
    print(f"Добавлено строк: {rows_added}")

    # Проверка, что сегмент всё ещё есть в списке (т.к. ack_delete не прошёл)
    segments_after = wpc.list_segments(HOST)
    seg_in_list = next((s for s in segments_after if s["name"] == seg["name"]), None)
    assert seg_in_list is not None, "Сегмент исчез из списка, хотя ack_delete должен был провалиться"

    # Проход 2 — штатный
    segments_new = wpc.list_segments(HOST)
    seg2 = next(s for s in segments_new if s["name"] == seg["name"])
    status2, rows2, gap2, diag2 = wpc.fetch_one_stitch(HOST, stitcher, seg2, token)

    print(f"Проход 2: status={status2}, rows={rows2}")
    assert status2 == "ok", f"Ожидается статус 'ok', но получено '{status2}'"
    assert rows2 == 0, "Строки задвоились"
    
    # Проверка, что количество строк не увеличилось
    assert stitcher.state.get("rows", 0) == rows_before + seg.get("rows"), "Количество строк изменилось непредсказуемо"

    # Проверка, что сегмент исчез из списка (т.к. ack_delete прошёл)
    segments_final = wpc.list_segments(HOST)
    seg_removed = next((s for s in segments_final if s["name"] == seg["name"]), None)
    assert seg_removed is None, "Сегмент не был удалён на штатном проходе"

    print("C3 ПРОЙДЕН: данные не потеряны и не задвоились при обрыве связи перед ack_delete")
    return 0

if __name__ == "__main__":
    sys.exit(main())
