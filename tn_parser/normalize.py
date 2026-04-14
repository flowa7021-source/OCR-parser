# -*- coding: utf-8 -*-
"""Нормализация текста перед парсингом.

Операции:
    1. Убираем мягкий перенос (U+00AD), BOM, zero-width-пробелы.
    2. Склеиваем слова, разорванные переносом: "при-\\nём" → "приём".
    3. Схлопываем подряд идущие пробельные символы (но не переводы строк —
       их использует сегментатор разделов).
    4. Чиним латинские символы, попавшие в кириллические слова (A→А, P→Р и т. п.).

Функция `normalize_for_sections` сохраняет структуру строк — её использует
сегментатор. Функция `collapse` сворачивает всё в одну строку — полезна для
запасных эвристик.
"""

from __future__ import annotations

import re
import unicodedata


# Типовые «невидимые» символы, которые встречаются в машиночитаемых PDF.
_INVISIBLE = {
    "\u00ad",  # soft hyphen
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\ufeff",  # BOM
}

# Перенос слова: буква, дефис, перевод строки, буква.
_HYPHEN_BREAK = re.compile(
    r"([А-Яа-яЁёA-Za-z])[-\u2010\u2011]\s*\n\s*([А-Яа-яЁёA-Za-z])"
)

# Пробелы/табы подряд (без учёта \n).
_HORIZ_WS = re.compile(r"[ \t\u00a0\u2009\u202f]+")

# Пустые строки подряд (2+ \n) → один \n.
_MULTI_NL = re.compile(r"\n{2,}")

# Символы-цифры в смешанных строках (для фильтра «мусор»).
_MEANINGFUL = re.compile(r"[А-Яа-яЁё0-9]")

# Латиница, визуально совпадающая с кириллицей.
_LAT_TO_CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
    "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
    "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р",
    "x": "х", "y": "у",
}
_MIXED_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def _strip_invisible(text: str) -> str:
    for ch in _INVISIBLE:
        if ch in text:
            text = text.replace(ch, "")
    return text


def _is_cyr(c: str) -> bool:
    return bool(c) and ("\u0400" <= c <= "\u04FF")


def _fix_confusables(text: str) -> str:
    """Меняем латинские буквы-двойники на кириллицу, но только когда
    соседние буквы в слове — кириллические.

    Это чинит OCR-артефакты вроде «ИHH» → «ИНН» и «Мapкa» → «Марка», но не
    трогает смешанные product-names типа «Тенsar», где латинский «a»
    окружён латинскими соседями (s, r).

    Идём слева направо, опираясь на уже сконвертированные предыдущие символы
    (чтобы второй H в «ИHH» увидел, что его левый сосед теперь Н).
    """

    def replace(m: re.Match[str]) -> str:
        chars = list(m.group(0))
        for i, c in enumerate(chars):
            if c in _LAT_TO_CYR:
                left = chars[i - 1] if i > 0 else ""
                right = chars[i + 1] if i + 1 < len(chars) else ""
                if _is_cyr(left) or _is_cyr(right):
                    chars[i] = _LAT_TO_CYR[c]
        return "".join(chars)

    return _MIXED_WORD.sub(replace, text)


def normalize_for_sections(text: str) -> str:
    """Мягкая нормализация: сохраняет переводы строк (нужны сегментатору)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _strip_invisible(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _HORIZ_WS.sub(" ", text)
    text = _MULTI_NL.sub("\n", text)
    text = _fix_confusables(text)
    return text.strip()


def collapse(text: str) -> str:
    """Жёсткая нормализация в одну строку."""
    text = normalize_for_sections(text)
    return text.replace("\n", " ").strip()


def is_garbage(s: str) -> bool:
    """Нет ни кириллицы, ни цифр — значит мусор."""
    return not _MEANINGFUL.search(s or "")


def clean_value(value: str, missing: str, garbage: str) -> str:
    """Обрезает мусорные хвосты и проверяет содержимое."""
    if not value:
        return missing
    v = value.strip(" \t\r\n:;,.-–—|")
    if not v:
        return missing
    if is_garbage(v):
        return garbage
    return v
