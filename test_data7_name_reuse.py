
import wf_pull_client as wpc
import os
from pathlib import Path

def test_digest_razlichaet_tezok():
    """Два разных blob одинаковой длины дают разные seg_digest."""
    blob_a = b"A" * 100
    blob_b = b"B" * 100
    assert wpc.seg_digest(blob_a) != wpc.seg_digest(blob_b)

def test_tezka_ne_priznan_dublem(tmp_path):
    """На Stitcher с искусственно проставленным state["ingested"] вызов ingest_confirmed возвращает False."""
    st = wpc.Stitcher(str(tmp_path / "shov.aswf"))
    blob_a = b"A" * 100
    blob_b = b"B" * 100
    digest_a = wpc.seg_digest(blob_a)
    digest_b = wpc.seg_digest(blob_b)
    st.state["ingested"]["seg_00006.aswf"] = {"b": 100, "d": digest_a}
    assert not st.ingest_confirmed("seg_00006.aswf", digest_b)

def test_nastoyashiy_dubl_priznan(tmp_path):
    """Тот же setup, но ingest_confirmed с дайджестом blob_A возвращает True."""
    st = wpc.Stitcher(str(tmp_path / "shov.aswf"))
    blob_a = b"A" * 100
    digest_a = wpc.seg_digest(blob_a)
    st.state["ingested"]["seg_00006.aswf"] = {"b": 100, "d": digest_a}
    assert st.ingest_confirmed("seg_00006.aswf", digest_a)

def test_state_starogo_formata_ne_podtverzhdaet(tmp_path):
    """Если запись имеет старый формат (просто int), ingest_confirmed возвращает False."""
    st = wpc.Stitcher(str(tmp_path / "shov.aswf"))
    st.state["ingested"]["seg_00006.aswf"] = 123
    assert not st.ingest_confirmed("seg_00006.aswf", "any_digest")

def test_stitch_kachaet_dazhe_pri_sovpadenii_imeni(monkeypatch):
    """Ключевой тест сценария: fetch_one_stitch вызывает http_get даже при совпадении имени."""
    class FakeStitcher:
        def __init__(self):
            self.append_segment_called = False

        def ingest_confirmed(self, name, digest):
            return False

        def note_sizemismatch(self, name):
            return 1

        def ingest_crc_bad(self, name):
            return False         # #DATA-8: сегмент не помечен битым

        def clear_sizemismatch(self, name):
            pass

        def append_segment(self, name, blob):
            self.append_segment_called = True
            return (4, None, {"crc_bad": 0, "crc_checked": 4, "seq_gap": 0, "recon": None})

    fake_stitcher = FakeStitcher()
    http_get_calls = []
    ack_delete_calls = []

    def mock_http_get(url, binary=True):
        http_get_calls.append(1)
        return b"B" * 100

    def mock_ack_delete(host, name, token):
        ack_delete_calls.append(1)
        return True

    monkeypatch.setattr(wpc, "http_get", mock_http_get)
    monkeypatch.setattr(wpc, "ack_delete", mock_ack_delete)

    seg = {"name": "seg_00006.aswf", "bytes": 100}
    status, rows, gap, diag = wpc.fetch_one_stitch("host", fake_stitcher, seg, "token")
    assert len(http_get_calls) == 1
    assert fake_stitcher.append_segment_called
    assert status == "ok"

def test_stitch_ne_ackaet_bez_skachivaniya(monkeypatch):
    """Если http_get бросает TimeoutError, ack_delete НЕ вызывается."""
    class FakeStitcher:
        def ingest_confirmed(self, name, digest):
            return False

    def mock_http_get(url, binary=True):
        raise TimeoutError("timeout")

    def mock_ack_delete(host, name, token):
        assert False, "Не должен быть вызван"

    monkeypatch.setattr(wpc, "http_get", mock_http_get)
    monkeypatch.setattr(wpc, "ack_delete", mock_ack_delete)

    seg = {"name": "seg_00006.aswf", "bytes": 100}
    status, rows, gap, diag = wpc.fetch_one_stitch("host", FakeStitcher(), seg, "token")
    assert status.startswith("error:get:")

def test_filemode_ne_zatiraet_tezku(tmp_path, monkeypatch):
    """fetch_one_filemode не затирает существующий файл с тем же именем.

    #DATA-8 место 4: с валидацией parse_aswf blob обязан быть настоящим ASWF,
    иначе тест падает на "bad magic" раньше проверки затирания — не по адресу."""
    blob_a = _make_seg([[10] * CH])
    blob_b = _make_seg([[20] * CH])
    file_path = tmp_path / "seg_00006.aswf"
    file_path.write_bytes(blob_a)
    calls = []

    def mock_http_get(url, binary=True):
        calls.append(1)
        return blob_b

    def mock_ack_delete(host, name, token):
        return True

    monkeypatch.setattr(wpc, "http_get", mock_http_get)
    monkeypatch.setattr(wpc, "ack_delete", mock_ack_delete)

    seg = {"name": "seg_00006.aswf", "bytes": len(blob_b)}
    wpc.fetch_one_filemode("host", str(tmp_path), seg, "token")

    assert file_path.read_bytes() == blob_a, "существующий файл затёрт тёзкой"
    assert len(calls) == 1, "скачивание пропущено по совпадению имени и размера"
    # новые данные обязаны лечь рядом, а не пропасть: ищем файл с содержимым blob_b
    saved = [p for p in tmp_path.iterdir()
             if p.is_file() and p.read_bytes() == blob_b]
    assert saved, "новый сегмент-тёзка нигде не сохранён — данные потеряны"
    assert saved[0] != file_path

def test_trim_ogranichivaet_ingested(tmp_path):
    """_trim_ingested удаляет старые записи, оставляя только последние INGEST_KEEP."""
    st = wpc.Stitcher(str(tmp_path / "shov.aswf"))
    for i in range(wpc.INGEST_KEEP + 50):
        st.state["ingested"][f"seg_{i:05d}.aswf"] = {"b": 100, "d": f"d{i}"}
    st._trim_ingested()
    last = f"seg_{wpc.INGEST_KEEP + 49:05d}.aswf"
    assert len(st.state["ingested"]) == wpc.INGEST_KEEP
    assert "seg_00000.aswf" not in st.state["ingested"], "удалена не самая старая запись"
    assert "seg_00050.aswf" in st.state["ingested"]
    assert last in st.state["ingested"], "выброшена свежая запись вместо старой"

import struct
import json
import zlib

# раскладка строки формата ASWF v5 (совпадает с тем, что пишет прошивка)
CH = 8192          # каналов спектра
STRIDE = 16410     # длина строки
SPEC_OFF = 0       # спектр
DUR_OFF = 16384    # длительность, uint16
TS_OFF = 16386     # абсолютный таймстамп строки, uint32
CRC_OFF = 16390    # crc32 строки, uint32; covers = CRC_OFF


def _make_seg(rows_counts, started_at=1700000000, seg_seq=0, stride=None, calibration=None):
    stride = stride or STRIDE      # другой stride = другой формат строки (#REC-14)
    # Строим заголовок
    header = {
        "version": 5,
        "channels": CH,
        "row_stride": stride,
        "saved_rows": 0,
        "started_at": started_at,
        "seg_seq": seg_seq,
        "total_at_open": 0,
        "interval_sec": 180,
        "calibration": calibration if calibration is not None else [0.0, 3.0, 0.0],
        "row_fields": [
            {"name": "spectrum", "offset": SPEC_OFF},
            {"name": "duration", "offset": DUR_OFF},
            {"name": "timestamp", "offset": TS_OFF},
            {"name": "crc32", "offset": CRC_OFF, "covers": CRC_OFF}
        ]
    }
    hj = json.dumps(header, separators=(",", ":"))
    hj_bytes = hj.encode("utf-8")
    body = bytearray()
    for i, row_data in enumerate(rows_counts):
        b = bytearray(stride)
        for j in range(CH):
            offset = SPEC_OFF + j * 2
            b[offset:offset+2] = struct.pack("<H", row_data[j])
        # Длительность строки
        b[DUR_OFF:DUR_OFF+2] = struct.pack("<H", 180)
        # Таймстамп
        b[TS_OFF:TS_OFF+4] = struct.pack("<I", started_at + i)
        # CRC32
        crc = zlib.crc32(bytes(b[:CRC_OFF])) & 0xFFFFFFFF
        b[CRC_OFF:CRC_OFF+4] = struct.pack("<I", crc)
        body.extend(b)
    # в поле после магии идёт длина ШАПКИ (hlen), а не всего сегмента:
    # parse_aswf читает payload по смещению 8 + hlen
    return b"ASWF" + struct.pack("<I", len(hj_bytes)) + hj_bytes + bytes(body)


def _payload_of(blob):
    """Полезная нагрузка сегмента (без магии, длины и шапки)."""
    hlen = struct.unpack_from("<I", blob, 4)[0]
    return blob[8 + hlen:]


def _mocks(monkeypatch, blob, acks):
    """Подменить сеть: скачивание отдаёт blob, вызовы ack копятся в acks."""
    monkeypatch.setattr(wpc, "http_get", lambda url, binary=False: blob)
    monkeypatch.setattr(wpc, "ack_delete",
                        lambda host, name, token: (acks.append(1), True)[1])


def test_e2e_tezka_posle_perenumeracii_vshivaetsya(monkeypatch, tmp_path):
    """Сценарий пострадавшего целиком, на настоящем Stitcher: сегмент-тёзка того же
    размера, но с другим содержимым обязан быть вшит, а не отброшен как дубль."""
    shov = tmp_path / "shov.aswf"
    st = wpc.Stitcher(str(shov))
    blob_a = _make_seg([[10] * CH, [11] * CH, [12] * CH, [13] * CH],
                       started_at=1700000000, seg_seq=5)
    st.append_segment("seg_00006.aswf", blob_a)
    size_before, rows_before = shov.stat().st_size, st.state["rows"]

    blob_b = _make_seg([[20] * CH, [21] * CH, [22] * CH, [23] * CH],
                       started_at=1700009999, seg_seq=7)
    assert len(blob_b) == len(blob_a), "тест бессмыслен: размеры сегментов разошлись"

    acks = []
    _mocks(monkeypatch, blob_b, acks)
    seg = {"name": "seg_00006.aswf", "bytes": len(blob_b)}
    status, rows, _, _ = wpc.fetch_one_stitch("host", st, seg, "token")

    assert status == "ok"
    assert rows == 4, "строки тёзки не вшиты — это и есть потеря данных"
    assert st.state["rows"] == rows_before + 4
    assert shov.stat().st_size == size_before + 4 * STRIDE, "файл шва не вырос"
    with open(shov, "rb") as f:                  # данные легли РЕАЛЬНО, а не счётчик
        f.seek(size_before)
        assert f.read() == _payload_of(blob_b), "в файл легли не строки сегмента B"
    assert acks == [1], "ack должен уйти ровно один раз, после записи"


def test_e2e_nastoyashiy_dubl_ne_dubliruet_stroki(monkeypatch, tmp_path):
    """Обратная сторона: настоящий повтор (тот же байт-в-байт сегмент) не должен
    дописывать строки второй раз. Иначе фикс лечил бы потерю ценой дублей."""
    shov = tmp_path / "shov.aswf"
    st = wpc.Stitcher(str(shov))
    blob_a = _make_seg([[10] * CH, [11] * CH, [12] * CH, [13] * CH],
                       started_at=1700000000, seg_seq=5)
    st.append_segment("seg_00006.aswf", blob_a)
    size_before, rows_before = shov.stat().st_size, st.state["rows"]

    acks = []
    _mocks(monkeypatch, blob_a, acks)
    seg = {"name": "seg_00006.aswf", "bytes": len(blob_a)}
    status, rows, _, _ = wpc.fetch_one_stitch("host", st, seg, "token")

    assert status == "ok"
    assert rows == 0, "тот же сегмент вшит повторно — дубли строк"
    assert st.state["rows"] == rows_before
    assert shov.stat().st_size == size_before, "файл шва вырос на повторе"
    assert acks == [1], "ack не отправлен: сегмент навсегда останется на плате"


def test_proverka_vnutri_rotacii_dostizhima(tmp_path):
    """Защита от дублей внутри append_segment при ротации формата (#REC-14) —
    НЕ мёртвый код: _rotate_for_format вызывает _load_state(), то есть подменяет
    state целиком на state ДРУГОГО файла шва. Внешняя проверка в fetch_one_stitch
    смотрела в прежний state и ответа за новый дать не может."""
    base = str(tmp_path / "shov.aswf")
    old = _make_seg([[10] * CH, [11] * CH], seg_seq=1, stride=16402)
    st = wpc.Stitcher(base)
    st.append_segment("seg_00001.aswf", old)

    new = _make_seg([[20] * CH, [21] * CH], seg_seq=2)      # stride 16410 -> ротация
    assert st.append_segment("seg_00002.aswf", new)[0] == 2, "сегмент не вшит после ротации"

    # рестарт клиента на исходном пути: state снова от ПЕРВОГО файла шва
    st2 = wpc.Stitcher(base)
    assert not st2.ingest_confirmed("seg_00002.aswf", wpc.seg_digest(new)), \
        "внешняя проверка знает про сегмент — тогда append_segment не вызвали бы"

    rows, _, _ = st2.append_segment("seg_00002.aswf", new)
    assert rows == 0, "строки продублированы: проверка внутри ротации не сработала"


