
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
    
    for seg in segments_before or []:
        if "bytes" in seg and "rows" in seg:
            expected_rows = (seg["bytes"] - args.seg_overhead) // args.stride
            if expected_rows != seg["rows"]:
                print(f"⚠ сегмент {seg['name']}: заявлено rows={seg['rows']}, из размера следует {expected_rows} — плата сообщает о себе неверно", file=sys.stderr)
    
    for seg in segments_after or []:
        if "bytes" in seg and "rows" in seg:
            expected_rows = (seg["bytes"] - args.seg_overhead) // args.stride
            if expected_rows != seg["rows"]:
                print(f"⚠ сегмент {seg['name']}: заявлено rows={seg['rows']}, из размера следует {expected_rows} — плата сообщает о себе неверно", file=sys.stderr)
    
    # Вывод
    diff = produced_by_board - expected_balance
    
    if args.json:
        result = {
            "produced_by_board": produced_by_board,
            "written_to_pc": rows_written,
            "dropped_by_ring": dropped_by_ring,
            "remaining_on_board": remaining_on_board,
            "expected_balance": expected_balance,
            "difference": diff
        }
        print(json.dumps(result, ensure_ascii=False))
        # код возврата обязан отражать результат и в этом режиме тоже:
        # без этого вызывающий скрипт увидит успех при несошедшемся балансе
        sys.exit(0 if diff == 0 else 1)
    else:
        print(f"произведено_платой: {produced_by_board}")
        print(f"записано_на_ПК: {rows_written}")
        print(f"вытеснено_кольцом: {dropped_by_ring}")
        print(f"осталось_на_плате: {remaining_on_board}")
        print(f"ожидаемый_баланс: {expected_balance}")
        if diff == 0:
            print("БАЛАНС СОШЁЛСЯ")
            sys.exit(0)
        else:
            print(f"БАЛАНС НЕ СОШЁЛСЯ: расхождение {diff} строк")
            sys.exit(1)

if __name__ == "__main__":
    main()
