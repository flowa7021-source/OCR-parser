# -*- coding: utf-8 -*-
"""Парсер транспортных накладных из PDF в Excel.

Десктопное приложение для Windows 10/11 на Python + Tkinter. Извлекает данные
российских транспортных накладных (ТН) из машиночитаемых PDF и сохраняет
результат в форматированный .xlsx.

Логика парсинга вынесена в пакет `tn_parser/`; этот модуль — тонкий GUI-слой.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from tn_parser import ParsedRow, extract_raw_text, process_one_pdf
from tn_parser.models import GARBAGE, MISSING


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

APP_TITLE = "Парсер транспортных накладных"
OUTPUT_FILENAME = "extraction.xlsx"
SHEET_NAME = "Extraction"

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
# Запись Excel
# ---------------------------------------------------------------------------


def write_excel(rows: List[ParsedRow], output_path: str) -> None:
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
        self.root.geometry("760x560")
        self.root.minsize(680, 500)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.worker_thread: Optional[threading.Thread] = None
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()

        # Для кнопки «Сырой текст»: последний список обработанных PDF.
        self._last_pdfs: List[str] = []

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

        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.run_btn = ttk.Button(
            actions, text="▶  Извлечь данные", command=self._on_run, state="disabled"
        )
        self.run_btn.pack(side="left")

        self.raw_btn = ttk.Button(
            actions, text="🔍  Сырой текст PDF…", command=self._on_show_raw
        )
        self.raw_btn.pack(side="left", padx=8)

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

    # ---- «Сырой текст» -----------------------------------------------------

    def _on_show_raw(self) -> None:
        """Показывает сырой текст выбранного PDF в отдельном окне.

        Удобно отлаживать парсинг: видно, что вытащил PyMuPDF, и почему
        регулярка могла не сработать.
        """
        initial = self.input_var.get().strip() or os.getcwd()
        pdf_path = filedialog.askopenfilename(
            title="Выберите PDF для просмотра",
            initialdir=initial,
            filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")],
        )
        if not pdf_path:
            return

        try:
            text = extract_raw_text(pdf_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Не удалось прочитать PDF: {exc}")
            return

        self._open_text_window(os.path.basename(pdf_path), text or "[пусто]")

    def _open_text_window(self, title: str, text: str) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"Сырой текст: {title}")
        win.geometry("820x620")
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        widget = ScrolledText(frame, wrap="word")
        widget.pack(fill="both", expand=True)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

        def copy_all() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

        bottom = ttk.Frame(win)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bottom, text="Копировать всё", command=copy_all).pack(side="right")
        ttk.Button(bottom, text="Закрыть", command=win.destroy).pack(side="right", padx=6)

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

        self._last_pdfs = pdfs
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

    def _worker(self, pdfs: List[str], out_path: str) -> None:
        t0 = time.time()
        total = len(pdfs)
        ok_count = 0
        err_count = 0
        results: Dict[str, List[ParsedRow]] = {}

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
                        rows = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        rows = [ParsedRow.empty_missing(fname, note=f"ERROR: {exc}")]
                    results[pdf_path] = rows

                    any_error = any(r.note.startswith("ERROR:") for r in rows)
                    if any_error:
                        err_count += 1
                        err_text = next(r.note for r in rows if r.note.startswith("ERROR:"))
                        self.msg_queue.put(("log", f"Ошибка: {fname} — {err_text[7:]}"))
                    else:
                        ok_count += 1
                        suffix = f" ({len(rows)} накладных)" if len(rows) > 1 else ""
                        self.msg_queue.put(("log", f"Обработано: {fname} — OK{suffix}"))

                    done += 1
                    self.msg_queue.put(("progress", done, total))

            # Сохраняем строки в исходном порядке.
            ordered_rows: List[ParsedRow] = []
            for p in pdfs:
                ordered_rows.extend(results.get(p, []))
            write_excel(ordered_rows, out_path)

            elapsed = time.time() - t0
            self.msg_queue.put(("log", "──────────────────────────"))
            self.msg_queue.put((
                "log",
                f"Итого: {total} файлов, {ok_count} OK, {err_count} ошибок, "
                f"{len(ordered_rows)} строк (за {elapsed:.1f} с)",
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
