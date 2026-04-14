# -*- coding: utf-8 -*-
"""Пакет парсинга транспортных накладных (ТН).

Верхнеуровневый API:
    from tn_parser import process_one_pdf, parse_text, ParsedRow

Разбивка по модулям:
    layout.py      — извлечение текста из PDF с учётом геометрии блоков
    normalize.py   — нормализация текста (переносы, confusables, пробелы)
    sections.py    — разбиение текста на пронумерованные разделы ТН (1–17)
    validators.py  — валидаторы ГРЗ / ИНН / даты
    fields.py      — извлечение конкретных полей из разделов
    splitter.py    — разбиение одного PDF на несколько накладных
    core.py        — склейка всего конвейера
    models.py      — датаклассы результата
"""

from .models import MISSING, GARBAGE, ParsedRow, FieldConfidence
from .core import process_one_pdf, parse_text, extract_raw_text

__all__ = [
    "MISSING",
    "GARBAGE",
    "ParsedRow",
    "FieldConfidence",
    "process_one_pdf",
    "parse_text",
    "extract_raw_text",
]
