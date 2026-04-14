# -*- coding: utf-8 -*-
"""Извлечение конкретных полей из разделов ТН.

Принцип: сначала ищем поле в «своём» разделе (высокая уверенность), если его
нет — падаем на эвристики по всему тексту (низкая уверенность). Каждый
экстрактор возвращает кортеж (value, confidence).
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from .normalize import clean_value, is_garbage
from .validators import (
    find_grz,
    find_inn,
    is_valid_date,
    is_valid_grz,
    is_valid_inn,
)
from .models import MISSING, GARBAGE


# Регулярки для организационно-правовой формы — это хорошие маркеры конца
# названия контрагента при «одной строкой».
_ORG_TERMINATORS = (
    r"\b(?:ИНН|КПП|ОГРН|ОКПО|адрес|тел\.?|телефон|e-?mail|\+7|т\.|факс)\b"
)
_ORG_TERMINATOR_RE = re.compile(_ORG_TERMINATORS, re.IGNORECASE)

_DATE_ANY = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_NUMBER_IN_HEADER = re.compile(
    r"(?:№|No\.?|N[º°]?)\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/]{0,48})",
    re.IGNORECASE,
)
_WAYBILL_HEADER = re.compile(
    r"транспортн(?:ая|ой)\s+накладн(?:ая|ой)", re.IGNORECASE
)


def _truncate_at_terminator(s: str, max_len: int = 250) -> str:
    """Обрезает строку у первого терминатора (ИНН, КПП, телефон и т. п.)."""
    if not s:
        return s
    s = s.strip()
    m = _ORG_TERMINATOR_RE.search(s)
    if m:
        s = s[: m.start()].rstrip(" \t,:;–—-.")
    return s[:max_len].strip()


def _value_or_missing(v: Optional[str]) -> Tuple[str, float]:
    v = clean_value(v or "", MISSING, GARBAGE)
    if v == MISSING:
        return MISSING, 0.0
    if v == GARBAGE:
        return GARBAGE, 0.0
    return v, 0.7  # значение найдено в своём разделе


# ---------------------------------------------------------------------------


def extract_number_and_date(
    head: str, full_text: str
) -> Tuple[str, float, str, float]:
    """Возвращает (number, conf_number, date, conf_date).

    Ищем в «шапке» ТН: `Транспортная накладная № 12345 от 01.02.2024`.
    """
    number = MISSING
    conf_num = 0.0
    date = MISSING
    conf_date = 0.0

    source = head if head else full_text
    if not source:
        return number, conf_num, date, conf_date

    # Ищем последовательность «Транспортная накладная № … от …»
    anchor = _WAYBILL_HEADER.search(source)
    if anchor:
        tail = source[anchor.end() : anchor.end() + 400]
        num_m = _NUMBER_IN_HEADER.search(tail)
        if num_m:
            candidate = num_m.group(1).strip(" .,:;")
            if not is_garbage(candidate):
                number = candidate
                conf_num = 0.9 if head else 0.6
        date_m = _DATE_ANY.search(tail)
        if date_m and is_valid_date(date_m.group(1)):
            date = date_m.group(1)
            conf_date = 1.0 if head else 0.7

    # Запасные эвристики по всему тексту.
    if number == MISSING and full_text:
        m = _NUMBER_IN_HEADER.search(full_text[:500])
        if m:
            candidate = m.group(1).strip(" .,:;")
            if not is_garbage(candidate):
                number = candidate
                conf_num = 0.5
    if date == MISSING and full_text:
        for m in _DATE_ANY.finditer(full_text):
            if is_valid_date(m.group(1)):
                date = m.group(1)
                conf_date = 0.5
                break

    return number, conf_num, date, conf_date


def extract_org(section_body: str, full_text: str, fallback_kw: str) -> Tuple[str, float]:
    """Извлекает название организации из раздела.

    Берёт первую непустую строку, обрезает у организационных терминаторов.
    Если раздел пуст — ищет ключевое слово в общем тексте.
    """
    if section_body:
        # Пропускаем служебные подзаголовки вроде "Полное наименование:".
        lines = [ln.strip(" \t:-–—") for ln in section_body.splitlines() if ln.strip()]
        for line in lines:
            low = line.lower()
            if low.startswith(("полное наименование", "сокращ", "фио", "ф.и.о", "наимен")):
                # Значение может быть на этой же строке после двоеточия.
                parts = re.split(r"[:\-–—]", line, maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    candidate = _truncate_at_terminator(parts[1])
                    if candidate and not is_garbage(candidate):
                        return candidate, 1.0
                continue
            candidate = _truncate_at_terminator(line)
            if candidate and not is_garbage(candidate) and len(candidate) > 2:
                return candidate, 0.9

    if full_text and fallback_kw:
        pat = re.compile(
            rf"{fallback_kw}\s*[:\-–—]?\s*([^\n\r]{{2,250}})", re.IGNORECASE
        )
        m = pat.search(full_text)
        if m:
            candidate = _truncate_at_terminator(m.group(1))
            if candidate and not is_garbage(candidate):
                return candidate, 0.5

    return MISSING, 0.0


def extract_cargo(section_body: str, full_text: str) -> Tuple[str, float]:
    if section_body:
        lines = [ln.strip() for ln in section_body.splitlines() if ln.strip()]
        cleaned: list[str] = []
        for ln in lines:
            low = ln.lower()
            # "Наименование: X" — оставляем X.
            if low.startswith(("наимен", "груз:")):
                parts = re.split(r"[:\-–—]", ln, maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    cleaned.append(parts[1].strip())
                continue
            # Чисто измерительные строки — пропускаем.
            if re.match(r"^(ед\.\s*изм|кол-во|количес|масс|объ[её]м)\b", low):
                continue
            cleaned.append(ln)
        if cleaned:
            joined = " ".join(cleaned[:3])
            joined = _truncate_at_terminator(joined, max_len=400)
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text:
        m = re.search(
            r"наименовани\w*\s+груз\w*\s*[:\-–—]?\s*([^\n\r]{2,400})",
            full_text, re.IGNORECASE,
        )
        if m:
            candidate = _truncate_at_terminator(m.group(1), max_len=400)
            if candidate and not is_garbage(candidate):
                return candidate, 0.5

    return MISSING, 0.0


def extract_vehicle(section_body: str, full_text: str) -> Tuple[str, float]:
    """Транспортное средство: ищем сначала ГРЗ (валидированный), затем марку."""
    # 1) ГРЗ в разделе 11.
    if section_body:
        grz = find_grz(section_body)
        if grz and is_valid_grz(grz):
            # Добавляем марку/модель, если она рядом.
            extra = []
            for line in section_body.splitlines():
                line = line.strip()
                if not line:
                    continue
                low = line.lower()
                if low.startswith(("марка", "модель", "тип")):
                    parts = re.split(r"[:\-–—]", line, maxsplit=1)
                    if len(parts) == 2 and parts[1].strip():
                        extra.append(parts[1].strip())
                        if len(extra) >= 2:
                            break
            if extra:
                return f"{grz} ({'; '.join(extra)})", 1.0
            return grz, 1.0

    # 2) ГРЗ где-то в тексте.
    grz = find_grz(full_text) if full_text else None
    if grz and is_valid_grz(grz):
        return grz, 0.7

    # 3) Эвристика «гос.номер …».
    if full_text:
        for pat in (
            r"гос\.?\s*номер\s*[:\-–—]?\s*([^\n\r]{2,40})",
            r"рег\.?\s*знак\s*[:\-–—]?\s*([^\n\r]{2,40})",
            r"государственн\w*\s+регистрационн\w*\s+номер\s*[:\-–—]?\s*([^\n\r]{2,40})",
        ):
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                candidate = clean_value(m.group(1), MISSING, GARBAGE)
                if candidate not in (MISSING, GARBAGE):
                    return candidate, 0.5

    return MISSING, 0.0


def extract_reception(section_body: str, full_text: str) -> Tuple[str, float]:
    if section_body:
        body = section_body.strip()
        # Отрезаем возможные хвосты следующего раздела, если сегментатор не справился.
        body = re.split(
            r"\n\s*(?:сдач\w* груз|выдач\w* груз|доставк\w* груз)",
            body, maxsplit=1, flags=re.IGNORECASE,
        )[0]
        body = body[:1200].strip()
        if body and not is_garbage(body):
            return body, 0.9

    if full_text:
        m = re.search(r"при[ёе]м\s+груз\w*", full_text, re.IGNORECASE)
        if m:
            chunk = full_text[m.end(): m.end() + 1200]
            end_m = re.search(
                r"(сдач\w*\s+груз\w*|выдач\w*\s+груз\w*|доставк\w*\s+груз\w*|отметк\w*)",
                chunk, re.IGNORECASE,
            )
            if end_m:
                chunk = chunk[: end_m.start()]
            cleaned = clean_value(chunk, MISSING, GARBAGE)
            if cleaned not in (MISSING, GARBAGE):
                return cleaned, 0.5

    return MISSING, 0.0


# ---------------------------------------------------------------------------


def extract_all(sections: Dict[int, str], full_text: str) -> Dict[str, Tuple[str, float]]:
    """Единая точка: прогоняет все экстракторы и возвращает словарь."""
    head = sections.get(0, "")
    sec = lambda n: sections.get(n, "")  # noqa: E731

    number, c_num, date, c_date = extract_number_and_date(head, full_text)

    shipper, c_shipper = extract_org(sec(1), full_text, "грузоотправитель")
    carrier, c_carrier = extract_org(sec(10), full_text, "перевозчик")
    cargo, c_cargo = extract_cargo(sec(3), full_text)
    vehicle, c_vehicle = extract_vehicle(sec(11), full_text)
    reception, c_reception = extract_reception(sec(6), full_text)

    return {
        "number": (number, c_num),
        "date": (date, c_date),
        "shipper": (shipper, c_shipper),
        "cargo": (cargo, c_cargo),
        "carrier": (carrier, c_carrier),
        "vehicle": (vehicle, c_vehicle),
        "reception": (reception, c_reception),
    }
