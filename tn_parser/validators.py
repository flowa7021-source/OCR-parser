# -*- coding: utf-8 -*-
"""Валидаторы для извлечённых полей."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional


# --- Дата -----------------------------------------------------------------

_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


def is_valid_date(s: str) -> bool:
    m = _DATE_RE.fullmatch(s.strip()) if s else None
    if not m:
        return False
    dd, mm, yyyy = map(int, m.groups())
    if not (1990 <= yyyy <= 2100):
        return False
    try:
        _dt.date(yyyy, mm, dd)
    except ValueError:
        return False
    return True


# --- ГРЗ (государственный регистрационный знак) --------------------------

# 1) Легковой: А123АА777 (3 цифры региона) или А123АА77 (2 цифры).
# 2) Прицеп/такси: АА1234 77 или АА123456.
# Разрешённые буквы (ГОСТ Р 50577): АВЕКМНОРСТУХ.
_GRZ_LETTERS = "АВЕКМНОРСТУХ"
_GRZ_MAIN = re.compile(
    rf"^[{_GRZ_LETTERS}]\d{{3}}[{_GRZ_LETTERS}]{{2}}\s?\d{{2,3}}$"
)
_GRZ_TRAILER = re.compile(
    rf"^[{_GRZ_LETTERS}]{{2}}\d{{4}}\s?\d{{2,3}}$"
)


def is_valid_grz(s: str) -> bool:
    if not s:
        return False
    candidate = s.upper().replace("  ", " ").strip()
    # Убираем вкрапления пробелов внутри ядра, сохраняя разрыв перед регионом.
    core = candidate
    return bool(_GRZ_MAIN.match(core) or _GRZ_TRAILER.match(core))


GRZ_CANDIDATE = re.compile(
    rf"[{_GRZ_LETTERS}]\d{{3}}[{_GRZ_LETTERS}]{{2}}\s?\d{{2,3}}|"
    rf"[{_GRZ_LETTERS}]{{2}}\d{{4}}\s?\d{{2,3}}"
)


def find_grz(text: str) -> Optional[str]:
    """Находит первое вхождение ГРЗ в тексте (uppercase-normalized)."""
    if not text:
        return None
    m = GRZ_CANDIDATE.search(text.upper())
    return m.group(0).strip() if m else None


_GRZ_MAIN_PARTS = re.compile(
    rf"^([{_GRZ_LETTERS}])(\d{{3}})([{_GRZ_LETTERS}]{{2}})\s?(\d{{2,3}})$"
)
_GRZ_TRAILER_PARTS = re.compile(
    rf"^([{_GRZ_LETTERS}]{{2}})(\d{{4}})\s?(\d{{2,3}})$"
)


def format_grz(grz: str) -> str:
    """Канонический вид ГРЗ с пробелами: «Р 814 НР 152»."""
    if not grz:
        return grz
    s = grz.upper().replace(" ", "")
    m = _GRZ_MAIN_PARTS.match(s)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(4)}"
    m = _GRZ_TRAILER_PARTS.match(s)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return grz


# --- ИНН -------------------------------------------------------------------


def is_valid_inn(s: str) -> bool:
    """Проверка контрольной суммы ИНН (10 или 12 цифр)."""
    if not s or not s.isdigit():
        return False
    digits = [int(c) for c in s]
    if len(digits) == 10:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        checksum = sum(d * w for d, w in zip(digits[:9], weights)) % 11 % 10
        return checksum == digits[9]
    if len(digits) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(d * w for d, w in zip(digits[:10], w1)) % 11 % 10
        c2 = sum(d * w for d, w in zip(digits[:11], w2)) % 11 % 10
        return c1 == digits[10] and c2 == digits[11]
    return False


_INN_CANDIDATE = re.compile(r"\b(\d{10}|\d{12})\b")


def find_inn(text: str) -> Optional[str]:
    if not text:
        return None
    for m in _INN_CANDIDATE.finditer(text):
        if is_valid_inn(m.group(1)):
            return m.group(1)
    return None
