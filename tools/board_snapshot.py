"""Снимок состояния платы для сверки баланса строк (фазы тест-плана).

Намеренно не использует код клиента: снимок должен быть независимым свидетелем.

Режимы:
  --light  только /api/waterfall/status и /api/system — НЕ занимают HEAVY-полосу,
           годятся во время прогона;
  (полный) дополнительно /api/waterfall/segments — берёт HEAVY-слот, поэтому
           снимается ТОЛЬКО в паузах: до старта прогона и после его завершения.
"""
import sys
import json
import time
import argparse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def get(host, path, timeout=10):
    with urllib.request.urlopen(host.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://atomspectra.local")
    ap.add_argument("--out", required=True)
    ap.add_argument("--light", action="store_true",
                    help="не трогать листинг сегментов (безопасно во время прогона)")
    ap.add_argument("--pc-rows", type=int, default=None,
                    help="строк в файле шва на момент снимка; для нового файла 0")
    args = ap.parse_args()

    snap = {"ts": int(time.time()), "host": args.host, "light": args.light}

    snap["status"] = get(args.host, "/api/waterfall/status")
    try:
        snap["system"] = get(args.host, "/api/system")
    except Exception as e:
        snap["system_error"] = f"{type(e).__name__}: {e}"

    if args.light:
        print("режим light: листинг сегментов не запрашивался, полная сверка невозможна",
              file=sys.stderr)
    else:
        seg = get(args.host, "/api/waterfall/segments", timeout=30)
        snap["segments"] = seg.get("segments", seg) if isinstance(seg, dict) else seg

    if args.pc_rows is not None:
        snap["pc_rows"] = args.pc_rows

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)

    st = snap["status"]
    print(f"снимок записан: {args.out}")
    print(f"  total_rows={st.get('total_rows')} seg_count={st.get('seg_count')} "
          f"seg_dropped={st.get('seg_dropped')} flash_full={st.get('flash_full')}")
    if "segments" in snap:
        fin = [s for s in snap["segments"] if s.get("finalized")]
        print(f"  сегментов в листинге: {len(snap['segments'])}, завершённых: {len(fin)}, "
              f"строк в завершённых: {sum(s.get('rows', 0) for s in fin)}")


if __name__ == "__main__":
    main()
