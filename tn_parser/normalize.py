# -*- coding: utf-8 -*-
"""Нормализация текста перед парсингом.

Операции:
    1. Убираем невидимые символы (soft hyphen, zero-width, BOM).
    2. Склеиваем слова, разорванные переносом: "при-\\nём" → "приём".
    3. Схлопываем подряд идущие горизонтальные пробелы (но не переводы
       строк — они нужны сегментатору).
    4. Склеиваем пустые строки (2+ \\n в 1).
    5. Чиним латинские confusables внутри кириллических слов (A→А, P→Р и т. п.).

ВАЖНО: мы НЕ применяем `unicodedata.normalize("NFKC", ...)` — NFKC превращает
`№` в две буквы `No`, а также ломает другие спец-символы ТН. Для нормализации
confusables этого и не нужно.
"""

from __future__ import annotations

import re


# Невидимые символы, встречающиеся в машиночитаемых PDF.
_INVISIBLE = {
    "\u00ad",  # soft hyphen
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\ufeff",  # BOM
    "\u202a",  # LRE
    "\u202c",  # PDF
}

# Все юникодные тире/дефисы, которые встречаются в ТН.
DASHES = "\u002d\u2010\u2011\u2012\u2013\u2014\u2015\u2212"

# Перенос слова через дефис на новой строке.
_HYPHEN_BREAK = re.compile(
    rf"([А-Яа-яЁёA-Za-z])[{DASHES}]\s*\n\s*([А-Яа-яЁёA-Za-z])"
)

# Пробелы/табы подряд (не \n).
_HORIZ_WS = re.compile(r"[ \t\u00a0\u2009\u202f]+")

# 2+ \n → один \n.
_MULTI_NL = re.compile(r"\n{2,}")

# Содержательный символ: кириллица, латиница или цифра. Латиницу считаем
# валидной, т.к. в ТН встречаются брэнды ТС (RENAULT, VOLVO, MAN), артикулы
# товаров (B30F300) и т. п.
_MEANINGFUL = re.compile(r"[А-Яа-яЁёA-Za-z0-9]")

# Латиница, визуально совпадающая с кириллицей (регистрозависимо).
_LAT_TO_CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
    "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
    "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р",
    "x": "х", "y": "у",
}
_MIXED_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def _is_cyr(c: str) -> bool:
    return bool(c) and ("\u0400" <= c <= "\u04FF")


def _strip_invisible(text: str) -> str:
    for ch in _INVISIBLE:
        if ch in text:
            text = text.replace(ch, "")
    return text


def _fix_confusables(text: str) -> str:
    """Замена латинских двойников на кириллицу только при кириллических
    соседях (см. test_mixed_script_product_name_preserved)."""

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
    """Мягкая нормализация с сохранением переводов строк."""
    if not text:
        return ""
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
    return not _MEANINGFUL.search(s or "")


def clean_value(value: str, missing: str, garbage: str) -> str:
    if not value:
        return missing
    v = value.strip(" \t\r\n:;,.-–—|")
    if not v:
        return missing
    if is_garbage(v):
        return garbage
    return v
