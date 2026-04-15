# -*- coding: utf-8 -*-
"""Связывающий модуль: PDF → список ParsedRow + кэш.

Публичные функции:
    extract_raw_text(pdf_path)    — сырой (но нормализованный) текст
    parse_text(text, source)      — список ParsedRow из текста
    process_one_pdf(pdf_path)     — основной вход для GUI
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import List, Optional

from .fields import extract_all
from .layout import extract_best_text
from .models import GARBAGE, MISSING, FieldConfidence, ParsedRow
from .normalize import normalize_for_sections
from .sections import split_sections
from .splitter import split_documents
from .claude_fallback import CONFIDENCE_THRESHOLD, claude_enhance


LOW_TEXT_THRESHOLD = 200  # символов
CACHE_VERSION = 11  # ↑ при изменении логики парсинга


# ---------------------------------------------------------------------------
# Кэш
# ---------------------------------------------------------------------------
# Каждая версия парсера пишет в собственный подкаталог «v<N>».
# Старые подкаталоги («v8», «v9» и т.д.) удаляются автоматически при запуске.
# Это гарантирует, что старый .exe никогда не прочтёт кэш, записанный новым.
# ---------------------------------------------------------------------------

_CACHE_BASE = os.path.join(tempfile.gettempdir(), "transport_parser_cache")


def _cache_dir() -> str:
    """Возвращает каталог кэша для *текущей* версии, создаёт при необходимости."""
    path = os.path.join(_CACHE_BASE, f"v{CACHE_VERSION}")
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup_old_cache_dirs() -> None:
    """Удаляет каталоги кэша от предыдущих версий."""
    try:
        if not os.path.isdir(_CACHE_BASE):
            return
        current = f"v{CACHE_VERSION}"
        for entry in os.listdir(_CACHE_BASE):
            if entry != current and entry.startswith("v"):
                stale = os.path.join(_CACHE_BASE, entry)
                try:
                    for f in os.listdir(stale):
                        os.remove(os.path.join(stale, f))
                    os.rmdir(stale)
                except OSError:
                    pass
    except OSError:
        pass


# Выполняем очистку один раз при импорте модуля.
_cleanup_old_cache_dirs()


def _file_signature(pdf_path: str) -> str:
    try:
        st = os.stat(pdf_path)
        raw = f"{os.path.abspath(pdf_path)}|{st.st_size}|{int(st.st_mtime)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
    except OSError:
        return ""


def _cache_get(pdf_path: str) -> Optional[List[ParsedRow]]:
    sig = _file_signature(pdf_path)
    if not sig:
        return None
    cache_path = os.path.join(_cache_dir(), sig + ".json")
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Двойная проверка: версия должна совпадать (защита от редких коллизий).
        if isinstance(data, dict) and data.get("_cv") == CACHE_VERSION:
            rows_data = data.get("rows", [])
        else:
            # Устаревший формат или чужая версия — не использовать.
            return None
        return [ParsedRow.from_json_dict(d) for d in rows_data]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _cache_put(pdf_path: str, rows: List[ParsedRow]) -> None:
    sig = _file_signature(pdf_path)
    if not sig:
        return
    cache_path = os.path.join(_cache_dir(), sig + ".json")
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"_cv": CACHE_VERSION, "rows": [r.to_json_dict() for r in rows]},
                fh,
                ensure_ascii=False,
            )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------


def extract_raw_text(pdf_path: str) -> str:
    """PDF → нормализованный текст с сохранением структуры строк."""
    raw = extract_best_text(pdf_path)
    return normalize_for_sections(raw)


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------


def _build_row(text: str, source: str) -> ParsedRow:
    sections = split_sections(text)
    fields = extract_all(sections, text)

    row = ParsedRow(source=source)
    row.number = fields["number"][0]
    row.date = fields["date"][0]
    row.shipper = fields["shipper"][0]
    row.consignee = fields["consignee"][0]
    row.cargo = fields["cargo"][0]
    row.volume = fields["volume"][0]
    row.carrier = fields["carrier"][0]
    row.vehicle = fields["vehicle"][0]
    row.reception = fields["reception"][0]

    row.confidence = FieldConfidence(
        date=fields["date"][1],
        number=fields["number"][1],
        shipper=fields["shipper"][1],
        consignee=fields["consignee"][1],
        cargo=fields["cargo"][1],
        carrier=fields["carrier"][1],
        vehicle=fields["vehicle"][1],
        reception=fields["reception"][1],
    )

    if row.number not in (MISSING, GARBAGE):
        row.waybill = f"Транспортная накладная № {row.number}"
    else:
        row.waybill = "Транспортная накладная"

    notes = []
    if len(text) < LOW_TEXT_THRESHOLD:
        notes.append("LOW_TEXT")
    if row.confidence.overall() < 0.4:
        notes.append("LOW_CONF")
    row.note = ";".join(notes)

    # Если regex-парсер не уверен — пробуем улучшить через Claude API.
    # claude_enhance — no-op если ANTHROPIC_API_KEY не задан или пакет не установлен.
    if row.confidence.overall() < CONFIDENCE_THRESHOLD:
        row = claude_enhance(row, text)

    return row


def parse_text(text: str, source: str) -> List[ParsedRow]:
    """Парсит нормализованный текст, возвращая одну или несколько строк."""
    if not text or not text.strip():
        return [ParsedRow.empty_missing(source, note="LOW_TEXT")]

    documents = split_documents(text)
    rows: List[ParsedRow] = []
    for i, doc in enumerate(documents):
        row_source = source if len(documents) == 1 else f"{source}#{i + 1}"
        rows.append(_build_row(doc, row_source))
    return rows


def process_one_pdf(pdf_path: str, use_cache: bool = True) -> List[ParsedRow]:
    """Полный цикл обработки одного PDF. Возвращает список ParsedRow."""
    fname = os.path.basename(pdf_path)

    if use_cache:
        cached = _cache_get(pdf_path)
        if cached is not None:
            return cached

    try:
        text = extract_raw_text(pdf_path)
        rows = parse_text(text, fname)
    except Exception as exc:  # noqa: BLE001
        return [ParsedRow.empty_missing(fname, note=f"ERROR: {exc}")]

    if use_cache:
        _cache_put(pdf_path, rows)
    return rows


# ---------------------------------------------------------------------------
# Пакетная обработка (общая для GUI и CLI)
# ---------------------------------------------------------------------------


def iter_pdfs(input_path: str) -> List[str]:
    """Принимает папку или один PDF, возвращает отсортированный список путей."""
    if os.path.isfile(input_path):
        return [input_path] if input_path.lower().endswith(".pdf") else []
    if os.path.isdir(input_path):
        return sorted(
            os.path.join(input_path, f)
            for f in os.listdir(input_path)
            if f.lower().endswith(".pdf")
            and os.path.isfile(os.path.join(input_path, f))
        )
    return []


def process_batch(
    pdfs: List[str],
    use_cache: bool = True,
    max_workers: Optional[int] = None,
    progress=None,
) -> "dict[str, List[ParsedRow]]":
    """Пакетная обработка: возвращает dict {pdf_path: [ParsedRow, ...]}.

    `progress` — опциональный callable(done, total) для обновления UI.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if max_workers is None:
        max_workers = min(8, max(2, (os.cpu_count() or 2)))

    results: "dict[str, List[ParsedRow]]" = {}
    total = len(pdfs)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_to_path = {
            pool.submit(process_one_pdf, p, use_cache): p for p in pdfs
        }
        for fut in as_completed(fut_to_path):
            p = fut_to_path[fut]
            try:
                results[p] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[p] = [
                    ParsedRow.empty_missing(os.path.basename(p), note=f"ERROR: {exc}")
                ]
            done += 1
            if progress is not None:
                try:
                    progress(done, total, p, results[p])
                except Exception:  # noqa: BLE001
                    pass

    # Сохраняем исходный порядок.
    return {p: results[p] for p in pdfs if p in results}
