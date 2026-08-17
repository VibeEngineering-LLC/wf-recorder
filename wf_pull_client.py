#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
#REC-11/#REC-12 pull-модель: PC-клиент забирает завершённые сегменты водопада с
платы и дозаписывает их строки в ЕДИНЫЙ .aswf-файл спектрограммы (шов на лету).

БЕЗОПАСНОСТЬ (зачем pull, а не push): все соединения — ИСХОДЯЩИЕ с ПК. На рабочем
компе НЕ открывается ни одного входящего порта и не создаётся ни одного правила
firewall. Плата ничего не инициирует; ПК сам опрашивает её и забирает данные.

Цикл одного прохода (#REC-12, режим по умолчанию):
  1. GET  /api/csrf-token                       -> токен для мутирующего запроса
  2. GET  /api/status                           -> t1/t2/t3 -> дозапись в temps.csv
  3. GET  /api/waterfall/segments               -> список сегментов (JSON)
  4. для каждого finalized-сегмента:
       GET  /api/waterfall/segment?name=...      -> сырой .aswf в память
       строки сегмента дозаписываются в единый spectrogram.aswf (fsync),
       сегмент отмечается в state.json; только после этого:
       POST /api/waterfall/segment/delete?name=  -> плата стирает сегмент с Flash
     (X-CSRF-Token в заголовке; удаляем ТОЛЬКО после успешной записи на диск)

Единый файл: шапка = шапка ПЕРВОГО сегмента (saved_rows=0 -> строки считаются из
размера файла, конвенция #FW-14), payload = конкатенация строк всех сегментов.
Пауза платы между сегментами детектируется по started_at шапки и печатается
предупреждением, но в файл не попадает. (В формате v5 у строк ЕСТЬ абсолютный
timestamp uint32, offset 16386 — клиент его пока не читает; прежняя редакция этого
абзаца утверждала, что таймстампов нет вовсе, что верно только для v2.)

Идемпотентность (#DATA-7): сегмент опознаётся по отпечатку СОДЕРЖИМОГО, а не по
имени. Имя seg_NNNNN уникально лишь в пределах текущего наполнения каталога платы —
номер восстанавливается сканом каталога при старте, поэтому после ребута на
опустевшем каталоге имена идут заново. Клиент качает сегмент всегда и удаляет его
на плате только после того, как строки записаны на диск или отпечаток совпал с уже
вшитым.

Температура: у платы нет per-row температуры (прибор отдаёт T1/T2/T3 ответом на
-inf раз в 30 мин, #FW-13). Клиент логирует её рядом: <stitch>.temps.csv
(unix_ts;iso;t1;t2;t3) — по строке на проход; мерж со строками — по времени.

Только стандартная библиотека (urllib). mDNS-хост atomspectra.local обходит
VPN-прокси, который может отдавать 503 на прямых LAN-адресах.

Примеры:
  python wf_pull_client.py --once
  python wf_pull_client.py --interval 60 --stitch D:\wf\night_run.aswf
  python wf_pull_client.py --no-stitch --out D:\wf_segments   # старый режим: пофайлово
"""
import argparse
import datetime
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request
import urllib.error
import zlib
from array import array

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8")

HTTP_TIMEOUT = 30  # сек на запрос; сегмент ~1 МБ по LAN укладывается с запасом


def http_get(url, headers=None, binary=False):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def http_post(url, headers=None):
    # тело пустое: имя сегмента идёт в query-строке (симметрично GET /segment)
    req = urllib.request.Request(url, data=b"", headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.status, r.read().decode("utf-8", "replace")


def get_csrf(host):
    tok = json.loads(http_get(host + "/api/csrf-token")).get("token", "")
    if len(tok) != 32:
        raise RuntimeError(f"плата вернула CSRF-токен неожиданной длины: {len(tok)}")
    return tok


def list_segments(host):
    return json.loads(http_get(host + "/api/waterfall/segments"))


# ---------------------------------------------------------------- .aswf разбор

def parse_aswf(blob, name):
    """-> (hdr dict, prefix bytes magic+hdr+baseline, payload bytes).

    v3: baseline-секция (если "baseline" в заголовке) включается в prefix,
    чтобы merged-файл оставался корректным ASWF v3.
    """
    if blob[:4] != b"ASWF":
        raise ValueError(f"{name}: bad magic {blob[:4]!r}")
    hlen = struct.unpack_from("<I", blob, 4)[0]
    hdr = json.loads(blob[8:8 + hlen].decode("utf-8"))
    baseline_bytes = 0
    if "baseline" in hdr:
        b = hdr["baseline"]
        baseline_bytes = b.get("channels", b.get("count", 0)) * 4
    payload_off = 8 + hlen + baseline_bytes
    return hdr, blob[:payload_off], blob[payload_off:]


def payload_rows_durs(payload, stride, name, hdr=None):
    """Целые строки + сумма длительностей. Некратный хвост (краш при записи)
    отбрасывается с предупреждением — в единый файл идут только целые строки.

    hdr: если передан, берёт смещение поля duration из row_fields (v3).
    v1/v2 fallback: duration в последних 2 байтах каждой строки (stride-2).
    """
    n_rows = len(payload) // stride
    rem = len(payload) % stride
    if rem:
        print(f"  ⚠ {name}: некратный хвост {rem} B отброшен (недописанная строка)")
    # v3: duration offset из row_fields; v1/v2: stride-2 (последние 2 байта строки)
    dur_off = stride - 2
    if hdr and "row_fields" in hdr:
        for f in hdr["row_fields"]:
            if f.get("name") == "duration":
                dur_off = f["offset"]
                break
    dur = 0
    for i in range(n_rows):
        dur += struct.unpack_from("<H", payload, i * stride + dur_off)[0]
    return payload[:n_rows * stride], n_rows, dur


def verify_rows(whole, stride, n_rows, hdr, name):
    """#DATA-1a: сверить per-row CRC32 и посчитать Σ bins (для reconciliation).

    -> (crc_bad, crc_checked, sum_bins). crc_checked=0 для v1..v3 (нет поля crc32) —
    целостность строк не проверяется, только Σ bins считается.
    """
    ch = hdr["channels"]
    crc_off = covers = None
    spec_off = 0
    for f in hdr.get("row_fields", []):
        nm = f.get("name")
        if nm == "crc32":
            crc_off = f["offset"]
            covers = f.get("covers", crc_off)
        elif nm == "spectrum":
            spec_off = f.get("offset", 0)
    crc_bad = crc_checked = 0
    sum_bins = 0
    for i in range(n_rows):
        base = i * stride
        a = array("H")
        a.frombytes(whole[base + spec_off: base + spec_off + ch * 2])
        if sys.byteorder != "little":
            a.byteswap()
        sum_bins += int(sum(a))
        if crc_off is not None:
            want = struct.unpack_from("<I", whole, base + crc_off)[0]
            got = zlib.crc32(whole[base: base + covers]) & 0xFFFFFFFF
            crc_checked += 1
            if got != want:
                crc_bad += 1
    return crc_bad, crc_checked, sum_bins


# ---------------------------------------------------------------- шов (#REC-12)

class Stitcher:
    """Единый .aswf + state.json (идемпотентность и учёт времени).

    state: {"ingested": {name: {"b": bytes, "d": sha256[:32]}}, "rows": N,
            "dur_sum": сек, "started_at": unix первого сегмента}

    #DATA-7: до этой версии значением было голое число байт. Такие записи читаются
    (совместимость), но подтверждением не считаются — отпечатка в них нет.
    """

    def __init__(self, path):
        self._set_paths(path)
        self._load_state()

    def _set_paths(self, path):
        self.path = path
        self.state_path = path + ".state.json"
        self.temps_path = path + ".temps.csv"

    def _load_state(self):
        path = self.path
        file_ok = os.path.exists(path) and os.path.getsize(path) >= 8
        if file_ok and os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            self._validate_schema()
        else:
            if os.path.exists(self.state_path) and not file_ok:
                print(f"  ⚠ {path} отсутствует/пуст, а {self.state_path} есть — "
                      f"файл шва перемещён/удалён вручную, начинаю новый (state сброшен)")
            self.state = {"ingested": {}, "rows": 0, "dur_sum": 0, "started_at": None}

    def _validate_schema(self):
        """#DATA-9: state.json чужой схемы (ручная правка, старая/сторонняя
        версия, повреждение) не должен ронять весь процесс KeyError-ом при
        первом же обращении. Гарантирует после вызова: self.state — dict с
        обязательными ключами правильных типов."""
        if not isinstance(self.state, dict):
            print(f"  ⚠ {self.state_path}: не объект (получен {type(self.state).__name__}) "
                  f"— state сброшен, начинаю заново")
            self.state = {}
        if not isinstance(self.state.get("ingested"), dict):
            self.state["ingested"] = {}
        for key, default in (("rows", 0), ("dur_sum", 0), ("started_at", None)):
            if key not in self.state:
                self.state[key] = default

    def note_sizemismatch(self, name):
        """#DATA-9: считаем ПОДРЯДНЫЕ sizemismatch по имени сегмента — транзиентная
        гонка записи проходит за 1-2 повтора, системная проблема платы не проходит
        никогда. Не персистентно (в память, не в state.json) — переживать рестарт
        процесса счётчику не нужно, цель — не молчать в ТЕКУЩЕМ долгом прогоне."""
        c = getattr(self, "_sizemismatch_counts", None)
        if c is None:
            c = self._sizemismatch_counts = {}
        c[name] = c.get(name, 0) + 1
        return c[name]

    def clear_sizemismatch(self, name):
        c = getattr(self, "_sizemismatch_counts", None)
        if c:
            c.pop(name, None)

    def _cal_suffix(self, calibration):
        """#DATA-8: короткий детерминированный отпечаток калибровки для имени
        файла — сама калибровка (список float) в имя не годится."""
        h = hashlib.sha256(json.dumps(calibration, sort_keys=True).encode()).hexdigest()
        return h[:8]

    def _rotated_path(self, stride, calibration=None):
        """#REC-14/#DATA-8: путь нового файла шва при смене формата прошивки
        и/или калибровки — <base>__s<stride>__c<hash><ext>. Существующие
        суффиксы __s.../__c... снимаются (не наслаиваются при повторной смене)."""
        d = os.path.dirname(self.path)
        root, ext = os.path.splitext(os.path.basename(self.path))
        for marker, is_suffix in (("__c", lambda s: len(s) == 8), ("__s", str.isdigit)):
            i = root.rfind(marker)
            if i != -1 and is_suffix(root[i + 3:]):
                root = root[:i]
        suffix = f"__s{stride}"
        if calibration is not None:
            suffix += f"__c{self._cal_suffix(calibration)}"
        return os.path.join(d, f"{root}{suffix}{ext}")

    def _rotate_for_epoch(self, new_ch, new_stride, old_ch, old_stride,
                           new_cal=None, cal_changed=False):
        """#REC-14/#DATA-8: заморозить текущий файл шва и переключиться на новый
        при смене формата строки И/ИЛИ калибровки. Старый .aswf/.state.json/
        .temps.csv остаются на диске нетронутыми -- решение об удалении
        сегмента на плате не должно зависеть от того, что успело поменяться
        в шапке прошивки."""
        old_path = self.path
        self._set_paths(self._rotated_path(new_stride, new_cal if cal_changed else None))
        self._load_state()
        parts = []
        if old_stride != new_stride or old_ch != new_ch:
            ch_note = "" if old_ch == new_ch else f", ch {old_ch}→{new_ch}"
            parts.append(f"формат: stride {old_stride}→{new_stride}{ch_note}")
        if cal_changed:
            parts.append(f"калибровка сегмента ≠ калибровке файла шва")
        print(f"  ⟳ #REC-14/#DATA-8 смена {' и '.join(parts)}; "
              f"файл шва заморожен ({os.path.basename(old_path)}), "
              f"новые строки → {os.path.basename(self.path)}")

    def _save_state(self):
        tmp = self.state_path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _trim_ingested(self):
        """#DATA-7: расписки живут не вечно. Назначение записи — пережить падение
        между fsync и ack, то есть секунды; дальше сегмента на плате уже нет.
        Бессрочное накопление превращает каждую запись в мину под будущую
        перенумерацию имён (у пострадавшего их набралось ~360). Держим последние
        INGEST_KEEP — с запасом на самый долгий проход.

        Порядок — по ПЕРВОЙ вставке ключа, а не по последнему обращению: dict не
        переносит существующий ключ в конец при переприсваивании. Значит запись,
        подтверждавшаяся многократно, вытесняется наравне с прочими. Оставлено
        осознанно: худший исход вытеснения — повторная вшивка строк, не потеря."""
        ing = self.state.get("ingested")
        if not isinstance(ing, dict) or len(ing) <= INGEST_KEEP:
            return
        for name in list(ing)[:len(ing) - INGEST_KEEP]:   # dict хранит порядок вставки
            del ing[name]

    def ingest_confirmed(self, name, digest):
        """Настоящий ли это дубль: совпал отпечаток содержимого. Для записей
        старого формата (только размер) отпечатка нет — считаем НЕ подтверждённым
        и вшиваем. Ложная дозапись строк восстановима, стирание на плате — нет."""
        rec = self.state["ingested"].get(name)
        return isinstance(rec, dict) and rec.get("d") == digest

    def ingest_crc_bad(self, name):
        """#DATA-8 (находка Codeaudit P1, 2026-08-17): признак порчи обязан жить
        в СОСТОЯНИИ, а не в diag одного прохода. Раньше удержание ack работало
        только на первом проходе: на втором ingest_confirmed возвращал True,
        diag обнулялся, held-ветка пропускалась и битый сегмент удалялся — тот
        же дефект, что чинили, только с задержкой на один интервал."""
        rec = self.state["ingested"].get(name)
        return isinstance(rec, dict) and bool(rec.get("crc_bad"))

    def append_segment(self, name, blob):
        """Дозаписать строки сегмента в единый файл.
        -> (rows_added, gap_sec|None, diag dict).

        diag: {crc_bad, crc_checked, seq_gap|None, recon|None}
          seq_gap: сколько сегментов пропущено по разрыву seg_seq (#DATA-1b),
                   0 = цепочка непрерывна, None = seg_seq нет в шапке (v<4).
          recon:   (#DATA-1c) для ПРЕДЫДУЩЕГО сегмента —
                   (device_delta, sum_bins, device_delta−sum_bins) или None.
                   #DATA-2: СПРАВОЧНАЯ метрика, не вердикт. Колебания diff —
                   граничное перетекание отсчётов между сегментами (< пуассон-шума).
                   Вердикт целостности = CRC32+seq+size; вход = hist_drop платы.
        """
        hdr, prefix, payload = parse_aswf(blob, name)
        ch = hdr["channels"]
        stride = hdr.get("row_stride", ch * 2)
        whole, n_rows, dur = payload_rows_durs(payload, stride, name, hdr)

        crc_bad, crc_checked, sum_bins = verify_rows(whole, stride, n_rows, hdr, name)

        # #DATA-1b: разрыв глобального seg_seq -> потеря сегмента (кольцо стёрло).
        seq_gap = None
        seq = hdr.get("seg_seq")
        if seq is not None:
            last_seq = self.state.get("last_seg_seq")
            if last_seq is not None:
                seq_gap = seq - last_seq - 1   # 0 = непрерывно, >0 = пропуск, <0 = дубль/реордер

        # #DATA-1c: reconciliation ПРЕДЫДУЩЕГО сегмента. Прибор-дельта событий =
        # total_at_open текущего − total_at_open предыдущего; сверяем с Σ bins пред.
        recon = None
        tao = hdr.get("total_at_open")
        last_tao = self.state.get("last_total_at_open")
        last_sum = self.state.get("last_sum_bins")
        # только для непрерывной цепочки (seq_gap==0): при пропуске дельта охватывает
        # и стёртые сегменты, сравнивать с одним Σ bins некорректно.
        if tao is not None and last_tao is not None and last_sum is not None \
                and (seq_gap == 0 or seq_gap is None):
            device_delta = tao - last_tao
            recon = (device_delta, last_sum, device_delta - last_sum)

        # #REC-14/#DATA-8: смена формата строки ИЛИ калибровки в прошивке
        # платы больше НЕ роняет запись и не вшивает строки под чужой шапкой.
        # Активный файл шва замораживается, новая эпоха пишется в отдельный
        # <base>__s<stride>[__c<hash>].aswf. Раньше калибровка только логи-
        # ровалась ("детектор сработал, ack всё равно ушёл") — строки реально
        # ложились в файл с чужой калибровкой, пики оказывались не на своих
        # энергиях при внешне валидных данных (#DATA-8).
        if os.path.exists(self.path):
            fhdr, fstride, fch = self._file_header()
            cal_changed = hdr.get("calibration") != fhdr.get("calibration")
            if fch != ch or fstride != stride or cal_changed:
                self._rotate_for_epoch(ch, stride, fch, fstride,
                                       new_cal=hdr.get("calibration"),
                                       cal_changed=cal_changed)
                # NB: это НЕ дубль внешней проверки из fetch_one_stitch, хотя выглядит
                # ею. _rotate_for_epoch выше вызвал _load_state() — self.state целиком
                # заменён на state ДРУГОГО файла шва. Внешняя проверка смотрела в прежний
                # state и про этот файл ничего знать не могла. Удаление ветки даёт дубли
                # строк при рестарте после ротации; закреплено тестом
                # test_proverka_vnutri_rotacii_dostizhima.
                if self.ingest_confirmed(name, seg_digest(blob)):
                    # уже вшит в новый файл (рестарт после ротации, ack платы не
                    # прошёл) — строки не дублируем, снаружи останется только ack
                    return 0, None, None

        gap = None
        if not os.path.exists(self.path):
            # первый сегмент задаёт шапку файла (saved_rows=0 -> derive-from-size)
            try:
                with open(self.path, "wb") as f:
                    f.write(prefix)
                    f.write(whole)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                # #DATA-8: обрыв посреди записи первого сегмента эпохи — не
                # оставлять огрызок; следующая попытка должна снова увидеть
                # "файла нет", а не дописывать поверх повреждённого начала.
                if os.path.exists(self.path):
                    os.remove(self.path)
                raise
            self.state["started_at"] = hdr.get("started_at")
        else:
            # #DATA-8: калибровка проверена и, при расхождении, обработана
            # ротацией ВЫШЕ (до этого блока) — сюда доходит уже согласованная.
            # детект паузы платы: цепочка от ПРЕДЫДУЩЕГО сегмента (ожидаемое начало
            # = его started_at + его длительность). started_at < 1e9 = часы платы
            # ещё не синхронизированы SNTP (наблюдался started_at=1) — не сравниваем.
            seg_start = hdr.get("started_at")
            last_end = self.state.get("last_end")
            if last_end and seg_start and seg_start > 1e9 and last_end > 1e9:
                delta = seg_start - last_end
                if abs(delta) > 2 * max(hdr.get("interval_sec", 60), 60):
                    gap = delta
            # #DATA-8: обрыв дозаписи не должен разъезжать границы строк — при
            # сбое посреди write/fsync откатываем файл к размеру ДО этого
            # сегмента, иначе следующая попытка допишет тот же блок ПОВЕРХ
            # огрызка и все смещения после перестанут быть кратны stride
            # (воспроизведено: 16 из 21 строки становятся нечитаемыми).
            pre_size = self._truncate_orphan_tail(stride, name)
            try:
                with open(self.path, "ab") as f:
                    f.write(whole)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                with open(self.path, "r+b") as tf:
                    tf.truncate(pre_size)
                raise

        rec = {"b": len(blob), "d": seg_digest(blob)}
        if crc_bad:
            rec["crc_bad"] = True      # #DATA-8: переживает проход, см. ingest_crc_bad
        self.state["ingested"][name] = rec
        self._trim_ingested()
        self.state["rows"] = self.state.get("rows", 0) + n_rows
        self.state["dur_sum"] = self.state.get("dur_sum", 0) + dur
        if hdr.get("started_at"):
            self.state["last_end"] = hdr["started_at"] + dur
        # #DATA-1b/1c: якорь для проверки следующего сегмента
        if seq is not None:
            self.state["last_seg_seq"] = seq
        if tao is not None:
            self.state["last_total_at_open"] = tao
        self.state["last_sum_bins"] = sum_bins
        self.state["crc_bad_total"] = self.state.get("crc_bad_total", 0) + crc_bad
        self._save_state()
        return n_rows, gap, {"crc_bad": crc_bad, "crc_checked": crc_checked,
                             "seq_gap": seq_gap, "recon": recon}

    def _truncate_orphan_tail(self, stride, name):
        """#DATA-8 (находка Codeaudit P2, 2026-08-17): срезать неполную строку в
        хвосте файла шва ДО дозаписи. Откат по OSError покрывает только сбои,
        которые мы успеваем перехватить; kill -9 и потеря питания ПК оставляют
        огрызок, а выполнить truncate некому. Дозапись через 'ab' легла бы
        ПОВЕРХ него, и огрызок оказался бы в СЕРЕДИНЕ файла — разбор .aswf
        отбрасывает только хвост, поэтому нечитаемым стало бы всё, что после.
        -> размер файла после возможной обрезки."""
        size = os.path.getsize(self.path)
        rem = (size - self._prefix_len()) % stride
        if not rem:
            return size
        with open(self.path, "r+b") as f:
            f.truncate(size - rem)
        print(f"  ⚠ #DATA-8 перед {name}: в файле шва найдена неполная строка "
              f"({rem} B) — обрезана. Признак аварийного завершения прошлого "
              f"процесса (kill/питание); целые строки не затронуты.")
        return size - rem

    def _prefix_len(self):
        """Длина шапки файла шва (magic+hlen+json[+baseline]) — граница, от
        которой payload обязан быть кратен stride."""
        with open(self.path, "rb") as f:
            hlen = struct.unpack_from("<I", f.read(8), 4)[0]
            hdr = json.loads(f.read(hlen).decode("utf-8"))
        n = 8 + hlen
        if "baseline" in hdr:
            b = hdr["baseline"]
            n += b.get("channels", b.get("count", 0)) * 4
        return n

    def _file_header(self):
        with open(self.path, "rb") as f:
            head = f.read(8)
            hlen = struct.unpack_from("<I", head, 4)[0]
            hdr = json.loads(f.read(hlen).decode("utf-8"))
        ch = hdr["channels"]
        return hdr, hdr.get("row_stride", ch * 2), ch

    def log_temps(self, host):
        """GET /api/status -> t1/t2/t3 в <stitch>.temps.csv (по строке на проход)."""
        try:
            st = json.loads(http_get(host + "/api/status"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            print(f"  ⚠ температура не получена: {e}")
            return None
        if "t1" not in st:
            print("  ⚠ /api/status без t1 (прибор не отдал -inf) — пропуск температуры")
            return None
        t1, t2, t3 = st["t1"], st["t2"], st["t3"]
        if t1 == 0 and t2 == 0 and t3 == 0:
            return None  # прибор ещё не отдал температуру (~раз в 30 мин); 0 не пишем
        now = time.time()
        iso = datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds")
        new = not os.path.exists(self.temps_path)
        with open(self.temps_path, "a", encoding="utf-8") as f:
            if new:
                f.write("unix_ts;iso;t1;t2;t3\n")
            f.write(f"{now:.0f};{iso};{t1};{t2};{t3}\n")
        return t1, t2, t3


# ---------------------------------------------------------------- проходы

INGEST_KEEP = 256   # #DATA-7: сколько последних расписок держать в state.json


def seg_digest(blob):
    """#DATA-7: отпечаток содержимого сегмента — единственный надёжный ключ
    идемпотентности. Имя (seg_NNNNN) уникально лишь в пределах текущего наполнения
    каталога платы: номер восстанавливается сканом каталога при старте, поэтому
    после ребута на опустевшем каталоге имена начинаются заново. Размер тоже не
    различает — при фиксированном interval_sec он у всех полных сегментов один."""
    return hashlib.sha256(blob).hexdigest()[:32]


def _spare_collision(dst, blob, name):
    """#DATA-7: не затирать уже забранный файл его ТЁЗКОЙ с другим содержимым.
    Совпадение имени больше ничего не гарантирует, поэтому при расхождении
    отпечатка новый сегмент кладётся рядом под уникальным именем, а не поверх."""
    if not os.path.exists(dst):
        return dst
    with open(dst, "rb") as f:
        if seg_digest(f.read()) == seg_digest(blob):
            return dst                     # тот же самый сегмент, перезапись безвредна
    root, ext = os.path.splitext(dst)
    dst = f"{root}__{seg_digest(blob)[:8]}{ext}"
    print(f"  ⚠ {name}: на диске тёзка с другим содержимым (плата перенумеровала "
          f"сегменты) -> сохраняю как {os.path.basename(dst)}")
    return dst


def ack_delete(host, name, token):
    st, _ = http_post(host + "/api/waterfall/segment/delete?name=" + name,
                      headers={"X-CSRF-Token": token})
    return st == 200


def fetch_one_filemode(host, out_dir, seg, token):
    """Старый режим (--no-stitch): сегмент отдельным файлом в out_dir.
    Возвращает 'ok' | 'sizemismatch' | 'error:<...>'."""
    name = seg["name"]
    want = int(seg["bytes"])
    dst = os.path.join(out_dir, name)

    # #DATA-7: качаем ВСЕГДА, как и в режиме шва. Прежде скачивание пропускалось,
    # если на диске лежал файл того же имени и размера — но имена переиспользуются
    # после ребута платы на пустом каталоге, а размер при постоянном interval_sec
    # одинаков у всех. Совпадал ТЁЗКА: ack ниже стирал новые данные на плате, на
    # диске оставался старый файл, и никто ничего не замечал.
    try:
        blob = http_get(host + "/api/waterfall/segment?name=" + name, binary=True)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return f"error:get:{e}"
    if len(blob) != want:
        return "sizemismatch"                     # не удаляем на плате — заберём позже
    try:
        # #DATA-8: файловый режим проверял только размер, не содержимое —
        # совпадение размера с HTML-страницей ошибки (прокси/VPN) шло прямиком
        # в ack. parse_aswf здесь только для валидации, сырой blob пишем как есть.
        parse_aswf(blob, name)
    except (ValueError, KeyError, UnicodeDecodeError, struct.error) as e:
        return f"error:badcontent:{e}"

    dst = _spare_collision(dst, blob, name)
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)                          # атомарная публикация

    # приём подтверждён (файл на диске == листинг) -> плата освобождает Flash
    try:
        ok = ack_delete(host, name, token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return f"error:del:{e}"
    return "ok" if ok else "error:del-status"


def fetch_one_stitch(host, stitcher, seg, token):
    """Режим шва (#REC-12): строки в единый файл, потом delete на плате.

    ВСЕГДА возвращает 4-tuple (status, rows, gap, diag):
      status: 'ok' | 'sizemismatch' | 'error:<...>'
      rows:   строк дозаписано (0 если уже вшит/ошибка)
      gap:    пауза платы в секундах или None
      diag:   dict целостности v4 {crc_bad, crc_checked, seq_gap, recon} или None
    """
    name = seg["name"]
    want = int(seg["bytes"])

    # #DATA-7: качаем ВСЕГДА. Прежде скачивание пропускалось при совпадении
    # (имя, размер) — и тогда ack ниже стирал на плате сегмент, содержимого которого
    # клиент даже не видел. Так пропали 66 сегментов: после ребута платы на пустом
    # каталоге имена пошли заново, а при постоянном interval_sec совпал и размер.
    # Экономия была только на редких повторах, цена — потеря данных.
    try:
        blob = http_get(host + "/api/waterfall/segment?name=" + name, binary=True)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return f"error:get:{e}", 0, None, None
    if len(blob) != want:
        n = stitcher.note_sizemismatch(name)
        if n >= 3:
            print(f"  ❗ #DATA-9: {name} sizemismatch {n} раз подряд — похоже на "
                  f"системную проблему платы, не на транзиентную гонку записи")
        return "sizemismatch", 0, None, None   # не удаляем — заберём в след. проходе
    stitcher.clear_sizemismatch(name)

    if stitcher.ingest_confirmed(name, seg_digest(blob)):
        rows, gap, diag = 0, None, None       # тот же самый сегмент: строки уже в шве
    else:
        try:
            rows, gap, diag = stitcher.append_segment(name, blob)
        except (ValueError, OSError) as e:
            return f"error:stitch:{e}", 0, None, None

    # #DATA-8: детектор CRC сработал — ack не уходит. Строки уже легли в шов
    # (с пометкой), но хорошая копия на плате обязана пережить эту находку:
    # удаление здесь стирало последний целый экземпляр данных, а битый
    # оставался единственным на диске. Проверяются ОБА источника: diag свежей
    # вшивки И флаг в state — иначе удержание жило бы ровно один проход
    # (находка Codeaudit P1: на 2-м проходе diag=None, ack уходил).
    if (diag and diag.get("crc_bad")) or stitcher.ingest_crc_bad(name):
        return "held:crc_bad", rows, gap, diag
    try:
        ok = ack_delete(host, name, token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return f"error:del:{e}", rows, gap, diag
    return ("ok" if ok else "error:del-status"), rows, gap, diag


def one_pass(host, out_dir, stitcher):
    token = get_csrf(host)
    if stitcher:
        t = stitcher.log_temps(host)
        if t:
            print(f"  T: t1={t[0]} t2={t[1]} t3={t[2]} -> {os.path.basename(stitcher.temps_path)}")
    segs = list_segments(host)
    pending = sorted([s for s in segs if s.get("finalized")], key=lambda s: int(s["idx"]))
    got = skipped = failed = held = rows_total = dup_ack = 0
    for seg in pending:
        if stitcher:
            r, rows, gap, diag = fetch_one_stitch(host, stitcher, seg, token)
        else:
            r = fetch_one_filemode(host, out_dir, seg, token)
            rows, gap, diag = 0, None, None
        if r == "ok":
            got += 1
            rows_total += rows
            if stitcher and not rows:
                dup_ack += 1          # #DATA-7: отпечаток сошёлся с уже вшитым
            extra = f" +{rows} строк" if rows else " (уже вшит, только ack)"
            print(f"  ✓ {seg['name']}  {seg['bytes']} B{extra}  стёрт на плате")
            if gap is not None:
                print(f"  ⚠ разрыв времени перед {seg['name']}: {gap:+.0f} с "
                      f"(пауза платы; в файле шва не отражена)")
            if diag:
                cb, cc = diag["crc_bad"], diag["crc_checked"]
                if cc:
                    print(f"    CRC32: {cc - cb}/{cc} OK"
                          + (f"  ✗ ПОРЧА {cb} строк!" if cb else ""))
                sg = diag["seq_gap"]
                if sg is not None and sg > 0:
                    # #DATA-11: клиент видит РАЗРЫВ СЧЁТЧИКА, а не факт потери.
                    # Прежний текст утверждал «потеряны безвозвратно» и вводил в
                    # заблуждение: у пользователя из issue #1 пропущенный сегмент
                    # остался на плате после таймаута, а сообщение объявило его
                    # потерянным. Три разные причины разрыва — три разных исхода.
                    print(f"    ⚠ #DATA-1b разрыв счётчика сегментов: пропущено {sg}")
                    if failed:
                        print(f"      выше в этом проходе были ошибки — вероятно, эти "
                              f"сегменты ЕЩЁ НА ПЛАТЕ, заберутся следующим проходом")
                    else:
                        print(f"      сегмента на плате нет. Причины: вытеснен кольцом "
                              f"(забор реже, чем плата пишет) либо оборван перезагрузкой "
                              f"(незавершённый сегмент не переживает ребут — так устроена "
                              f"запись). Сверьте seg_dropped в /api/waterfall/status")
                elif sg is not None and sg < 0:
                    print(f"    ⚠ #DATA-1b seg_seq откат {sg} (дубль/реордер)")
                rc = diag["recon"]
                if rc is not None:
                    dev, sb, d = rc  # d = device_delta − Σbins
                    # #DATA-2 (решение оператора 2026-07-09): recon — СПРАВОЧНАЯ метрика,
                    # НЕ вердикт целостности. Гарантия транспорта = CRC32(строка) +
                    # seq(сегмент) + фикс-размер (проверки выше); входная потеря
                    # детектор→плата = счётчик hist_drop в /api/spectrum.json.
                    # Колебания d — граничное перетекание отсчётов между сегментами
                    # (зазор чтения прибор-total vs финализация; < пуассон-шума,
                    # взаимозачёт по соседям) → метки ✗/⚠ здесь были ложными тревогами.
                    print(f"    recon пред.сегм (справочно): прибор Δ={dev}, "
                          f"Σbins={sb}, d={d:+d}")
        elif r == "sizemismatch":
            skipped += 1
            print(f"  ~ {seg['name']}  размер не сошёлся — повтор в след. проходе")
        elif r == "held:crc_bad":
            # #DATA-8: строки вшиты (с пометкой), но плата НЕ очищена —
            # решение об удалении следует за детектором, а не идёт параллельно.
            held += 1
            if diag:
                cb, cc = diag["crc_bad"], diag["crc_checked"]
                print(f"  ⚠ {seg['name']}  {seg['bytes']} B  CRC32 {cb}/{cc} строк "
                      f"битые — вшито с пометкой, ack УДЕРЖАН (плата не очищена)")
            else:
                # повторный проход: строки уже в шве, признак порчи взят из state
                print(f"  ⚠ {seg['name']}  {seg['bytes']} B  помечен битым ранее — "
                      f"ack УДЕРЖАН, сегмент намеренно оставлен на плате")
        else:
            failed += 1
            print(f"  ✗ {seg['name']}  {r}")
    open_cnt = sum(1 for s in segs if not s.get("finalized"))
    tail = ""
    if stitcher:
        tail = (f", файл шва: {stitcher.state.get('rows', 0)} строк / "
                f"{stitcher.state.get('dur_sum', 0)} с")
    # #DATA-7: проход, где плата отдала сегменты, а в шов не легло ни строки, —
    # аномалия. Именно так выглядела молчаливая потеря 66 сегментов: строка итога
    # печатала «+0 строк, ошибок 0», и это читалось как успех.
    if dup_ack and not rows_total:
        print(f"  ⚠ ВНИМАНИЕ: {dup_ack} сегм. подтверждено по отпечатку и стёрто на "
              f"плате, в шов добавлено 0 строк. Это нормально только если вы "
              f"перезапустили забор уже забранных данных.")
    print(f"проход: забрано {got} (+{rows_total} строк), отложено {skipped}, "
          f"удержано {held} (CRC), ошибок {failed}, открытых (пропущены) {open_cnt}{tail}")
    return got, failed


def main():
    ap = argparse.ArgumentParser(
        description="Pull-клиент водопада: сегменты платы -> единый .aswf (#REC-11/#REC-12)")
    ap.add_argument("--host", default="http://atomspectra.local",
                    help="базовый URL платы (default: mDNS, обходит VPN-прокси)")
    ap.add_argument("--out", default=None,
                    help="рабочая папка (default: <script>/../received)")
    ap.add_argument("--stitch", default=None, metavar="FILE",
                    help="путь единого .aswf (default: <out>/spectrogram.aswf)")
    ap.add_argument("--no-stitch", action="store_true",
                    help="старый режим: каждый сегмент отдельным файлом, без шва")
    ap.add_argument("--interval", type=int, default=60,
                    help="секунд между проходами (default 60)")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    args = ap.parse_args()

    host = args.host.rstrip("/")
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "received")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    stitcher = None
    if not args.no_stitch:
        stitch_path = os.path.abspath(args.stitch or os.path.join(out_dir, "spectrogram.aswf"))
        stitcher = Stitcher(stitch_path)
        print(f"host={host}  шов={stitch_path}  interval={args.interval}s  once={args.once}")
    else:
        print(f"host={host}  out={out_dir} (пофайлово)  interval={args.interval}s  once={args.once}")

    while True:
        try:
            one_pass(host, out_dir, stitcher)
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, TimeoutError) as e:
            print(f"проход не удался: {e}")
        except KeyboardInterrupt:
            # #DATA-10: Ctrl+C — штатный способ остановки, а не сбой. Простыня
            # traceback пугает и заставляет думать, что данные пострадали.
            # Строки уже на диске (fsync до ack), состояние согласовано.
            print("\nостановлено пользователем (Ctrl+C); записанное на диске цело")
            return 0
        if args.once:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nостановлено пользователем (Ctrl+C)")
            return 0
    return 0


if __name__ == "__main__":
    main()
