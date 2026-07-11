# wf_recorder — рекордер спектрограмм AtomSpectra

Настольная программа (Windows) для непрерывной **записи водопада** (спектрограммы)
с WiFi-шлюза гамма-спектрометра **AtomSpectra** на ESP32-S3.

Основной проект (прошивка шлюза): <https://github.com/VibeEngineering-LLC/atomspectra-waterfall-esp32>

## Что делает

Тонкий pull-клиент поверх HTTP-API шлюза:

- забирает сегменты водопада с платы и сшивает их в **единый файл `.aswf`** (+ `temps.csv`
  с температурами);
- удаляет сегмент на плате **только после `fsync` на диск ПК** — данные не теряются при
  обрыве;
- контроль целостности формата v4/v5: **CRC32 + порядковые номера сегментов** (#REC-13),
  авто-ротация файла шва при смене формата прошивки (#REC-14);
- окно на `tkinter` (только стандартная библиотека, внешних зависимостей нет): адрес платы,
  файл записи, интервал опроса, Старт/Стоп, счётчики (строк, длительность, температура,
  сегментов на плате) и журнал.

## Запуск

Готовый `wf_recorder.exe` — в разделе [Releases](../../releases) (сборка PyInstaller, без
установки Python).

Из исходников:

```bat
:: двойной клик
wf_recorder.bat

:: или напрямую
python wf_recorder_app.py --host http://<IP-платы> --interval 60
```

Требуется Python 3.8+ (только stdlib). Служебные флаги самопроверки: `--autostart`,
`--exit-after N`, `--stitch FILE`.

## Сборка exe

```bat
pip install pyinstaller
pyinstaller wf_recorder.spec
:: результат: dist\wf_recorder.exe
```

## Состав

| Файл | Назначение |
|---|---|
| `wf_recorder_app.py` | UI-программа (tkinter) поверх pull-клиента |
| `wf_pull_client.py` | логика забора/шва сегментов (Stitcher, CSRF, list/get/delete) |
| `wf_recorder.spec` | спецификация PyInstaller (bundle + `wf_recorder.exe`) |
| `wf_recorder.bat` | лаунчер (UTF-8, `python`/`py -3`) |

## Лицензия

MIT © VibeEngineering LLC. См. [`LICENSE`](LICENSE).
