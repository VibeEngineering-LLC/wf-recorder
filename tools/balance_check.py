
import sys
import json
import argparse
import os
import struct

sys.stdout.reconfigure(encoding="utf-8")

def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    if "flash_rows" in data.get("status", {}):
        print("⚠ поле flash_rows проигнорировано (завышает, см. spectrogram.c:903)", file=sys.stderr)
        del data["status"]["flash_rows"]
    
    return data

def read_aswf_header(path):
    with open(path, "rb") as f:
        sig = f.read(4)
        if sig != b"ASWF":
            raise ValueError("Неверная сигнатура файла ASWF")
        
        hlen = struct.unpack("<I", f.read(4))[0]
        header_data = f.read(hlen)
        header = json.loads(header_data.decode("utf-8"))
        
        row_stride = header.get("row_stride")
        channels = header.get("channels", 0)
        baseline_offset = 0
        if "baseline" in header:
            baseline_offset = channels * 4
        
        payload_offset = 8 + hlen + baseline_offset
        file_size = os.path.getsize(path)
        payload_size = file_size - payload_offset
        
        if payload_size < 0:
            raise ValueError("Неверный размер файла ASWF")
        
        rows = payload_size // row_stride
        remainder = payload_size % row_stride
        
        if remainder != 0:
            print(f"⚠ некратный хвост: {remainder} байт в конце файла", file=sys.stderr)
        
        return rows, row_stride, channels

def get_finalized_rows(segments):
    if segments is None:
        return 0
    return sum(seg["rows"] for seg in segments if seg.get("finalized", False))

def get_open_rows(segments):
    if segments is None:
        return 0
    return sum(seg["rows"] for seg in segments if not seg.get("finalized", False))

def has_open_segment(segments):
    """#TEST-1: был ли на снимке открытый (finalized=false) сегмент.

    API платы отдаёт rows=0 для НЕЗАВЕРШЁННОГО сегмента ВСЕГДА, даже если в
    нём реально уже накоплены строки — поле обновляется только при
    финализации. Поэтому агрегатная формула через total_rows/finalized-суммы
    не может быть точной, если окно before/after задевает границу открытого
    сегмента — это слепое пятно измерения, не потеря данных.
    """
    return any(not seg.get("finalized", False) for seg in (segments or []))

def reconcile_by_identity(segments_before, segments_after):
    """#TEST-1: сверка по СУДЬБЕ каждого сегмента, не по агрегатным суммам.

    Единственная достоверная величина API — rows у УЖЕ завершённого сегмента.
    Сегмент finalized=true в before, пропавший из листинга after, считается
    переданным клиенту и удалённым с платы — его rows суммируются. Открытые
    сегменты (в before или в after) не входят в подсчёт вовсе — про их
    реальное наполнение достоверно ничего не известно.
    """
    before_finalized = {s["name"]: s["rows"] for s in (segments_before or [])
                         if s.get("finalized", False)}
    after_names = {s["name"] for s in (segments_after or [])}
    transferred = {name: rows for name, rows in before_finalized.items()
                   if name not in after_names}
    still_present = [name for name in before_finalized if name in after_names]
    return sum(transferred.values()), sorted(transferred), still_present

def bounded_consistency(transferred_rows, rows_written, produced_by_board, seg_rows=64):
    """#TEST-1 (A2): вердикт для сегментов, рождённых И потреблённых ВНУТРИ окна.

    reconcile_by_identity видит только то, что было finalized уже в 'before' —
    сегмент, целиком возникший и переданный в паузе между снимками, ей не
    виден вовсе. Единственная проверка, которая тут доступна: избыток
    (rows_written сверх известного backlog) должен объясняться либо
    производством за окно (total_rows delta), либо скрытым буфером открытого
    сегмента на ОДНОЙ из двух границ — а тот не может превышать (seg_rows-1)
    строк по определению (иначе сегмент уже финализировался бы).
    """
    excess = rows_written - transferred_rows
    if excess < 0:
        return "MISSING", excess, None  # известный backlog не весь долетел — жёсткий провал
    if excess == 0:
        return "EXACT", excess, None  # весь избыток отсутствует — born-and-consumed нет вовсе
    slack = excess - produced_by_board
    bound = seg_rows - 1
    if -bound <= slack <= bound:
        return "BOUNDED", excess, slack  # объяснимо скрытым буфером, реальной потери не видно
    return "UNEXPLAINED", excess, slack  # выходит за физически возможное — настоящая находка

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--seam", required=True)
    parser.add_argument("--state")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--seg-overhead", type=int, default=36872)
    parser.add_argument("--stride", type=int, default=16410)
    
    args = parser.parse_args()
    
    before = load_snapshot(args.before)
    after = load_snapshot(args.after)
    
    # Проверка наличия необходимых полей
    if "status" not in before or "status" not in after:
        print("Ошибка: отсутствует поле status в одном из снимков", file=sys.stderr)
        sys.exit(2)
    
    # Считаем строки в шве
    try:
        total_rows_after, row_stride, channels = read_aswf_header(args.seam)
    except Exception as e:
        print(f"Ошибка чтения файла шва: {e}", file=sys.stderr)
        sys.exit(2)
    
    # Получаем строки до из снимка
    # pc_rows лежит на ВЕРХНЕМ уровне снимка, не внутри status. И проверять надо
    # наличие ключа, а не истинность значения: pc_rows=0 — законное значение
    # (файл шва начат с нуля), а не признак отсутствия.
    if "pc_rows" in before:
        pc_rows_before = before["pc_rows"]
    else:
        pc_rows_before = 0
        print("⚠ в before-снимке нет pc_rows, баланс считается от начала файла шва",
              file=sys.stderr)
    
    # Считаем количество строк в шве
    rows_written = total_rows_after - pc_rows_before
    
    # Считаем данные из снимков
    before_total_rows = before["status"]["total_rows"]
    after_total_rows = after["status"]["total_rows"]
    before_seg_dropped = before["status"]["seg_dropped"]
    after_seg_dropped = after["status"]["seg_dropped"]
    
    produced_by_board = after_total_rows - before_total_rows
    dropped_by_ring = after_seg_dropped - before_seg_dropped
    
    # Считаем строки в finalized-сегментах
    before_finalized = get_finalized_rows(before.get("segments"))
    after_finalized = get_finalized_rows(after.get("segments"))
    
    # Считаем оставшиеся на плате строки
    remaining_on_board = after_finalized
    
    # Вычисляем баланс
    expected_balance = rows_written + dropped_by_ring + remaining_on_board
    
    # Проверка согласованности метаданных сегментов
    segments_before = before.get("segments")
    segments_after = after.get("segments")

    # #TEST-1: надёжная сверка — по идентичности сегментов, не по агрегату
    transferred_rows, transferred_names, still_present = reconcile_by_identity(
        segments_before, segments_after)
    open_seg_present = has_open_segment(segments_before) or has_open_segment(segments_after)
    verdict_kind, excess, slack = bounded_consistency(
        transferred_rows, rows_written, produced_by_board)

    for seg in segments_before or []:
        if seg.get("bytes", 0) > 0 and "rows" in seg:
            expected_rows = (seg["bytes"] - args.seg_overhead) // args.stride
            if expected_rows != seg["rows"]:
                print(f"⚠ сегмент {seg['name']}: заявлено rows={seg['rows']}, из размера следует {expected_rows} — плата сообщает о себе неверно", file=sys.stderr)
    
    for seg in segments_after or []:
        if seg.get("bytes", 0) > 0 and "rows" in seg:
            expected_rows = (seg["bytes"] - args.seg_overhead) // args.stride
            if expected_rows != seg["rows"]:
                print(f"⚠ сегмент {seg['name']}: заявлено rows={seg['rows']}, из размера следует {expected_rows} — плата сообщает о себе неверно", file=sys.stderr)
    
    # Вывод. Главный вердикт — trёхзначный (см. bounded_consistency): различает
    # «точно сошлось», «сошлось в пределах физически возможного скрытого буфера
    # открытого сегмента» и «настоящее расхождение». Агрегатная арифметика
    # (diff/expected_balance) остаётся СПРАВОЧНОЙ, вердикт не решает.
    diff = produced_by_board - expected_balance
    verdict_pass = verdict_kind in ("EXACT", "BOUNDED")

    if args.json:
        result = {
            "verdict": verdict_kind,
            "identity_transferred_rows": transferred_rows,
            "excess_rows": excess,
            "slack": slack,
            "open_segment_present": open_seg_present,
            "produced_by_board": produced_by_board,
            "written_to_pc": rows_written,
            "dropped_by_ring": dropped_by_ring,
            "remaining_on_board": remaining_on_board,
            "expected_balance": expected_balance,
            "aggregate_difference": diff
        }
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if verdict_pass else 1)
    else:
        print(f"перенесено сегментов (по идентичности): {len(transferred_names)}, "
              f"строк: {transferred_rows}")
        print(f"записано_на_ПК: {rows_written}")
        if open_seg_present:
            print("⚠ на границе окна есть открытый сегмент — агрегатные числа ниже "
                  "справочные (API не сообщает накопление в незавершённом сегменте)")
        print(f"  справочно: произведено_платой={produced_by_board} "
              f"вытеснено_кольцом={dropped_by_ring} осталось_на_плате={remaining_on_board} "
              f"агрегатное_расхождение={diff}")
        if verdict_kind == "EXACT":
            print("БАЛАНС СОШЁЛСЯ ТОЧНО (по идентичности сегментов)")
        elif verdict_kind == "BOUNDED":
            print(f"БАЛАНС СОШЁЛСЯ В ПРЕДЕЛАХ ФИЗИЧЕСКОГО (избыток {excess} строк объясним "
                  f"скрытым буфером открытого сегмента на границе окна, slack={slack})")
        elif verdict_kind == "MISSING":
            print(f"БАЛАНС НЕ СОШЁЛСЯ: известный backlog {transferred_names} "
                  f"({transferred_rows} строк) не весь долетел до ПК (записано {rows_written})")
        else:
            print(f"БАЛАНС НЕ СОШЁЛСЯ: избыток {excess} строк выходит за физически "
                  f"возможный предел (slack={slack}, произведено_платой={produced_by_board})")
        sys.exit(0 if verdict_pass else 1)

if __name__ == "__main__":
    main()
