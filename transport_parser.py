# -*- coding: utf-8 -*-
"""
Парсер транспортных накладных из PDF в Excel.

Десктопное приложение для Windows 10/11 на Python + Tkinter.
Извлекает данные российских транспортных накладных из машиночитаемых PDF
по регулярным выражениям и сохраняет результат в форматированный .xlsx.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

import fitz  # PyMuPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

APP_TITLE = "Парсер транспортных накладных"
OUTPUT_FILENAME = "extraction.xlsx"
SHEET_NAME = "Extraction"
LOW_TEXT_THRESHOLD = 200  # символов
CACHE_VERSION = 2  # увеличить при изменении логики парсинга

COLUMNS = [
    ("Транспортная накладная", 30),
    ("Дата", 14),
    ("№", 15),
    ("Грузоотправитель", 35),
    ("Груз", 35),
    ("Перевозчик", 35),
    ("Транспортное средство", 22),
    ("Прием груза", 40),
    ("Источник файл", 25),
    ("Примечание", 18),
]

# ---------------------------------------------------------------------------
# Прекомпилированные regex-ы (компилируются один раз при импорте)
# ---------------------------------------------------------------------------

_RE_FLAGS = re.IGNORECASE | re.UNICODE

# Номер документа — по приоритету
RE_NUMBER = [
    re.compile(r"транспортн(?:ая|ой)\s+накладн(?:ая|ой)\s*№\s*([^\n\r]{1,50})", _RE_FLAGS),
    re.compile(r"\bнакладн(?:ая|ой)\b\s*№\s*([^\n\r]{1,50})", _RE_FLAGS),
    re.compile(r"(?:№|\bN\b)\s*([A-Za-zА-Яа-я0-9\-_/]+)", _RE_FLAGS),
]

# Дата
RE_DATE_LABELED = re.compile(r"дата\s*[:№N\-–— ]*\s*(\d{2}\.\d{2}\.\d{4})", _RE_FLAGS)
RE_DATE_ANY = re.compile(r"(\d{2}\.\d{2}\.\d{4})")

# Грузоотправитель
RE_SHIPPER = re.compile(r"грузоотправитель\s*[:\-–—]?\s*([^\n\r]{1,250})", _RE_FLAGS)

# Груз
RE_CARGO = [
    re.compile(r"наименовани(?:е|я)\s+груз(?:а|ов)\s*[:\-–—]?\s*([^\n\r]{1,400})", _RE_FLAGS),
    re.compile(r"\bгруз\b\s*[:\-–—]?\s*([^\n\r]{1,400})", _RE_FLAGS),
]

# Перевозчик
RE_CARRIER = re.compile(r"перевозчик\s*[:\-–—]?\s*([^\n\r]{1,250})", _RE_FLAGS)

# Транспортное средство
RE_VEHICLE = [
    re.compile(r"гос\.?\s*номер\s*[:\-–—]?\s*([^\n\r]{1,40})", _RE_FLAGS),
    re.compile(r"государственн\w*\s+регистрационн\w*\s+номер\s*[:\-–—]?\s*([^\n\r]{1,40})", _RE_FLAGS),
    re.compile(r"рег\.?\s*знак\s*[:\-–—]?\s*([^\n\r]{1,40})", _RE_FLAGS),
    re.compile(r"транспортн\w*\s+средств\w*\s*[:\-–—]?\s*([^\n\r]{1,80})", _RE_FLAGS),
]

# Приём груза
RE_RECEPTION_HEADER = re.compile(r"при[ёе]м\s+груз\w*", _RE_FLAGS)
RE_RECEPTION_END = re.compile(
    r"(сдач\w*\s+груз\w*|выдач\w*\s+груз\w*|доставк\w*\s+груз\w*|отметк\w*)",
    _RE_FLAGS,
)

# Нормализация пробелов
RE_WHITESPACE = re.compile(r"\s+")

# Проверка мусора
RE_MEANINGFUL = re.compile(r"[А-Яа-яЁё0-9]")

MISSING = "отсутствует"
GARBAGE = "неразборчиво"


# ---------------------------------------------------------------------------
# Кэш результатов парсинга
# ---------------------------------------------------------------------------


def _cache_dir() -> str:
    """Папка кэша в %TEMP% (или эквиваленте). Создаётся при первом доступе."""
    path = os.path.join(tempfile.gettempdir(), "transport_parser_cache")
    os.makedirs(path, exist_ok=True)
    return path


def _file_signature(pdf_path: str) -> str:
    """Сигнатура файла = sha1(абс.путь | размер | mtime). Дёшево, но надёжно."""
    try:
        st = os.stat(pdf_path)
        raw = f"{os.path.abspath(pdf_path)}|{st.st_size}|{int(st.st_mtime)}|v{CACHE_VERSION}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
    except OSError:
        return ""


def cache_get(pdf_path: str) -> Optional["ParsedRow"]:
    sig = _file_signature(pdf_path)
    if not sig:
        return None
    cache_path = os.path.join(_cache_dir(), sig + ".json")
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return ParsedRow(**data)
    except (OSError, ValueError, TypeError):
        return None


def cache_put(pdf_path: str, row: "ParsedRow") -> None:
    sig = _file_signature(pdf_path)
    if not sig:
        return
    cache_path = os.path.join(_cache_dir(), sig + ".json")
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(row), fh, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Структура одной строки результата
# ---------------------------------------------------------------------------


@dataclass
class ParsedRow:
    waybill: str = ""
    date: str = ""
    number: str = ""
    shipper: str = ""
    cargo: str = ""
    carrier: str = ""
    vehicle: str = ""
    reception: str = ""
    source: str = ""
    note: str = ""

    def to_excel_tuple(self):
        return (
            self.waybill,
            self.date,
            self.number,
            self.shipper,
            self.cargo,
            self.carrier,
            self.vehicle,
            self.reception,
            self.source,
            self.note,
        )


# ---------------------------------------------------------------------------
# Извлечение текста и парсинг полей
# ---------------------------------------------------------------------------


def extract_text_from_pdf(pdf_path: str) -> str:
    """Открывает PDF и извлекает текст со всех страниц (без OCR)."""
    doc = fitz.open(pdf_path)
    try:
        parts = []
        for page in doc:
            text = page.get_text("text") or ""
            parts.append(text)
        return "\n".join(parts)
    finally:
        doc.close()


def is_garbage(s: str) -> bool:
    """Нет ни кириллицы, ни цифр — значит мусор."""
    return not RE_MEANINGFUL.search(s or "")


def _clean(value: str) -> str:
    """Зачищает мусорные хвосты по краям + проверка на is_garbage."""
    if not value:
        return MISSING
    v = value.strip(" \t\r\n:;,.-–—|")
    if not v:
        return MISSING
    if is_garbage(v):
        return GARBAGE
    return v


def _first_match(patterns, text: str) -> Optional[str]:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def parse_fields(raw_text: str, source_filename: str) -> ParsedRow:
    """Парсит нормализованный текст одного PDF в ParsedRow."""
    row = ParsedRow(source=source_filename)

    if not raw_text:
        row.waybill = "Транспортная накладная"
        row.date = MISSING
        row.number = MISSING
        row.shipper = MISSING
        row.cargo = MISSING
        row.carrier = MISSING
        row.vehicle = MISSING
        row.reception = MISSING
        row.note = "LOW_TEXT"
        return row

    text = RE_WHITESPACE.sub(" ", raw_text).strip()

    # Номер
    raw_number = _first_match(RE_NUMBER, text)
    row.number = _clean(raw_number) if raw_number else MISSING

    # Колонка «Транспортная накладная»
    if row.number not in (MISSING, GARBAGE):
        row.waybill = f"Транспортная накладная № {row.number}"
    else:
        row.waybill = "Транспортная накладная"

    # Дата
    m = RE_DATE_LABELED.search(text)
    if m:
        row.date = m.group(1)
    else:
        m = RE_DATE_ANY.search(text)
        row.date = m.group(1) if m else MISSING

    # Грузоотправитель
    m = RE_SHIPPER.search(text)
    row.shipper = _clean(m.group(1)) if m else MISSING

    # Груз
    raw_cargo = _first_match(RE_CARGO, text)
    row.cargo = _clean(raw_cargo) if raw_cargo else MISSING

    # Перевозчик
    m = RE_CARRIER.search(text)
    row.carrier = _clean(m.group(1)) if m else MISSING

    # Транспортное средство
    raw_vehicle = _first_match(RE_VEHICLE, text)
    row.vehicle = _clean(raw_vehicle) if raw_vehicle else MISSING

    # Приём груза
    m = RE_RECEPTION_HEADER.search(text)
    if m:
        chunk = text[m.end(): m.end() + 1200]
        end_m = RE_RECEPTION_END.search(chunk)
        if end_m:
            chunk = chunk[: end_m.start()]
        row.reception = _clean(chunk)
    else:
        row.reception = MISSING

    # Примечание
    if len(text) < LOW_TEXT_THRESHOLD:
        row.note = "LOW_TEXT"
    else:
        row.note = ""

    return row


def process_one_pdf(pdf_path: str) -> ParsedRow:
    """Полный цикл: кэш → извлечь текст → распарсить → сохранить в кэш."""
    fname = os.path.basename(pdf_path)

    cached = cache_get(pdf_path)
    if cached is not None:
        return cached

    try:
        raw = extract_text_from_pdf(pdf_path)
        row = parse_fields(raw, fname)
    except Exception as exc:  # noqa: BLE001
        row = ParsedRow(
            waybill="Транспортная накладная",
            date=MISSING,
            number=MISSING,
            shipper=MISSING,
            cargo=MISSING,
            carrier=MISSING,
            vehicle=MISSING,
            reception=MISSING,
            source=fname,
            note=f"ERROR: {exc}",
        )
        return row  # ошибки не кэшируем

    cache_put(pdf_path, row)
    return row


# ---------------------------------------------------------------------------
# Запись Excel
# ---------------------------------------------------------------------------


def write_excel(rows: list[ParsedRow], output_path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME

    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    body_font = Font(name="Arial", size=10)
    body_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    headers = [c[0] for c in COLUMNS]
    ws.append(headers)
    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = border

    for row in rows:
        ws.append(row.to_excel_tuple())

    last_row = ws.max_row
    last_col = len(COLUMNS)
    for r in range(2, last_row + 1):
        for c in range(1, last_col + 1):
            cell = ws.cell(row=r, column=c)
            cell.font = body_font
            cell.alignment = body_align
            cell.border = border

    for idx, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(last_col)}{max(last_row, 1)}"

    wb.save(output_path)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class ParserApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("720x520")
        self.root.minsize(640, 480)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.worker_thread: Optional[threading.Thread] = None
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()

        self._build_ui()
        self._poll_queue()

    # ---- UI ----------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        frame_top = ttk.Frame(self.root)
        frame_top.pack(fill="x", **pad)

        ttk.Label(frame_top, text="Папка с PDF:").grid(row=0, column=0, sticky="w")
        in_entry = ttk.Entry(frame_top, textvariable=self.input_var)
        in_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frame_top, text="Обзор…", command=self._choose_input).grid(row=0, column=2)

        ttk.Label(frame_top, text="Сохранить в:").grid(row=1, column=0, sticky="w")
        out_entry = ttk.Entry(frame_top, textvariable=self.output_var)
        out_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frame_top, text="Обзор…", command=self._choose_output).grid(row=1, column=2)

        frame_top.columnconfigure(1, weight=1)

        self.run_btn = ttk.Button(
            self.root, text="▶  Извлечь данные", command=self._on_run, state="disabled"
        )
        self.run_btn.pack(pady=6)

        self.input_var.trace_add("write", lambda *_: self._refresh_run_state())
        self.output_var.trace_add("write", lambda *_: self._refresh_run_state())

        log_frame = ttk.LabelFrame(self.root, text="Лог")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(log_frame, height=14, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        ttk.Label(bottom, text="Прогресс:").pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.progress_label = ttk.Label(bottom, text="0/0")
        self.progress_label.pack(side="left")

    def _choose_input(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку с PDF")
        if path:
            self.input_var.set(path)
            if not self.output_var.get():
                self.output_var.set(path)

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Папка для сохранения")
        if path:
            self.output_var.set(path)

    def _refresh_run_state(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        if self.input_var.get().strip():
            self.run_btn.configure(state="normal")
        else:
            self.run_btn.configure(state="disabled")

    # ---- Логирование -------------------------------------------------------

    def _log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "progress":
                    done, total = msg[1], msg[2]
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.progress_label.configure(text=f"{done}/{total}")
                elif kind == "done":
                    self.run_btn.configure(state="normal")
                    self._refresh_run_state()
                elif kind == "error":
                    messagebox.showerror(APP_TITLE, msg[1])
                elif kind == "info":
                    messagebox.showinfo(APP_TITLE, msg[1])
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # ---- Запуск обработки --------------------------------------------------

    def _on_run(self) -> None:
        in_dir = self.input_var.get().strip()
        out_dir = self.output_var.get().strip() or in_dir

        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showwarning(APP_TITLE, "Выберите существующую папку с PDF.")
            return
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"Не удалось создать папку: {exc}")
                return

        pdfs = sorted(
            os.path.join(in_dir, f)
            for f in os.listdir(in_dir)
            if f.lower().endswith(".pdf") and os.path.isfile(os.path.join(in_dir, f))
        )
        if not pdfs:
            messagebox.showwarning(APP_TITLE, "В выбранной папке нет PDF-файлов.")
            return

        self.run_btn.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress["value"] = 0
        self.progress_label.configure(text=f"0/{len(pdfs)}")

        out_path = os.path.join(out_dir, OUTPUT_FILENAME)
        self.worker_thread = threading.Thread(
            target=self._worker, args=(pdfs, out_path), daemon=True
        )
        self.worker_thread.start()

    def _worker(self, pdfs: list[str], out_path: str) -> None:
        t0 = time.time()
        total = len(pdfs)
        ok_count = 0
        err_count = 0
        results: dict[str, ParsedRow] = {}

        # Параллельная обработка: PyMuPDF освобождает GIL во время чтения PDF.
        max_workers = min(8, max(2, (os.cpu_count() or 2)))
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_to_path = {pool.submit(process_one_pdf, p): p for p in pdfs}
                done = 0
                for fut in as_completed(future_to_path):
                    pdf_path = future_to_path[fut]
                    fname = os.path.basename(pdf_path)
                    try:
                        row = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        row = ParsedRow(
                            waybill="Транспортная накладная",
                            date=MISSING, number=MISSING, shipper=MISSING,
                            cargo=MISSING, carrier=MISSING, vehicle=MISSING,
                            reception=MISSING, source=fname,
                            note=f"ERROR: {exc}",
                        )
                    results[pdf_path] = row

                    if row.note.startswith("ERROR:"):
                        err_count += 1
                        self.msg_queue.put(("log", f"Ошибка: {fname} — {row.note[7:]}"))
                    else:
                        ok_count += 1
                        self.msg_queue.put(("log", f"Обработано: {fname} — OK"))

                    done += 1
                    self.msg_queue.put(("progress", done, total))

            # Сохраняем строки в исходном порядке (по сортированному списку файлов)
            ordered_rows = [results[p] for p in pdfs]
            write_excel(ordered_rows, out_path)

            elapsed = time.time() - t0
            self.msg_queue.put(("log", "──────────────────────────"))
            self.msg_queue.put((
                "log",
                f"Итого: {total} файлов, {ok_count} OK, {err_count} ошибок (за {elapsed:.1f} с)",
            ))
            self.msg_queue.put(("log", f"Сохранено: {out_path}"))
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=4)
            self.msg_queue.put(("log", f"Критическая ошибка: {exc}\n{tb}"))
            self.msg_queue.put(("error", f"Сбой обработки: {exc}"))
        finally:
            self.msg_queue.put(("done",))


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def main() -> int:
    root = tk.Tk()
    try:
        # Приоритетно — нативная тема Windows, иначе clam.
        style = ttk.Style(root)
        themes = style.theme_names()
        for preferred in ("vista", "winnative", "clam"):
            if preferred in themes:
                style.theme_use(preferred)
                break
    except tk.TclError:
        pass
    ParserApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
