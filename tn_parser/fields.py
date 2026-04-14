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
    is_valid_date,
    is_valid_grz,
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

# Служебные строки, которые надо пропускать в начале раздела «Грузоотправитель»:
# «является экспедитором» (с чекбоксом), «(реквизиты, позволяющие …)», и т. п.
_SERVICE_LINE_RE = re.compile(
    r"^(?:"
    r"является\s+(?:экспедитором|грузоотправителем)"
    r"|\(\s*реквизиты\b"
    r"|да\b|нет\b"
    r"|\[[ xх×✓✔]?\]"   # чекбоксы
    r"|полное\s+наименование"
    r"|сокращ\w*\s+наименование"
    r"|наименование\s+(?:юр|лица)"
    r"|фио|ф\.и\.о"
    r")",
    re.IGNORECASE,
)


def _truncate_at_terminator(s: str, max_len: int = 250) -> str:
    """Обрезает строку у первого терминатора (ИНН, КПП, телефон и т. п.).

    ВАЖНО: для ТН мы, наоборот, хотим оставить реквизиты (ИНН/КПП/адрес)
    в значении поля «Грузоотправитель»/«Грузополучатель», потому что именно
    они однозначно идентифицируют контрагента. Поэтому по умолчанию функция
    возвращает строку БЕЗ обрезки и используется только там, где хвост —
    шум следующего раздела.
    """
    if not s:
        return s
    return s[:max_len].strip()


def _trim_organization(s: str, max_len: int = 400) -> str:
    """Чуть более агрессивная обрезка — для одиночной строки 'после двоеточия',
    где дальше идёт мусор."""
    if not s:
        return s
    s = s.strip()
    m = _ORG_TERMINATOR_RE.search(s)
    # Оставляем ИНН/КПП — это часть ценных реквизитов. Обрезаем только телефон
    # и явные «мусорные» маркеры.
    return s[:max_len].strip()


# ---------------------------------------------------------------------------


def extract_number_and_date(
    head: str, full_text: str
) -> Tuple[str, float, str, float]:
    """Возвращает (number, conf_number, date, conf_date)."""
    number = MISSING
    conf_num = 0.0
    date = MISSING
    conf_date = 0.0

    source = head if head else full_text
    if not source:
        return number, conf_num, date, conf_date

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


def _skip_service_lines(lines):
    """Пропускает «мусорные» служебные строки в начале раздела контрагента."""
    result = []
    for ln in lines:
        if _SERVICE_LINE_RE.match(ln.strip()):
            # Если на той же строке после заглушки есть значение — сохраняем его.
            m = re.split(r"[:\-–—]", ln, maxsplit=1)
            if len(m) == 2 and m[1].strip() and not _SERVICE_LINE_RE.match(m[1].strip()):
                result.append(m[1].strip())
            continue
        result.append(ln)
    return result


def extract_org(section_body: str, full_text: str, fallback_kw: str) -> Tuple[str, float]:
    """Извлекает реквизиты контрагента из раздела.

    Берёт первые непустые строки (пропуская чекбоксы и подсказки), склеивает
    до 3 первых — чтобы получить «ООО … адрес … ИНН …».
    """
    if section_body:
        lines = [ln.strip(" \t") for ln in section_body.splitlines() if ln.strip()]
        lines = _skip_service_lines(lines)
        # Отбрасываем одиночные поясняющие строки вида "(реквизиты ...)".
        lines = [ln for ln in lines if not re.match(r"^\(.+\)$", ln)]
        if lines:
            # Склеиваем первые 3 строки — обычно этого хватает на название +
            # адрес + ИНН/КПП. Дальше идут телефоны и подписи, которые нам
            # в колонке не нужны.
            joined = ", ".join(ln.rstrip(",") for ln in lines[:3])
            joined = joined[:500].strip(" ,;")
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text and fallback_kw:
        pat = re.compile(
            rf"{fallback_kw}\s*[:\-–—]?\s*([^\n\r]{{2,350}})", re.IGNORECASE
        )
        m = pat.search(full_text)
        if m:
            candidate = m.group(1).strip()
            if (candidate and not is_garbage(candidate)
                    and not _SERVICE_LINE_RE.match(candidate)):
                return candidate, 0.5

    return MISSING, 0.0


def extract_cargo(section_body: str, full_text: str) -> Tuple[str, float]:
    if section_body:
        lines = [ln.strip() for ln in section_body.splitlines() if ln.strip()]
        cleaned: list[str] = []
        for ln in lines:
            low = ln.lower()
            # "1. Наименование — Блок …" или "Наименование: X" — берём X.
            m = re.match(
                r"^\s*(?:\d+\.\s*)?наимен\w*\s*[:\-–—]\s*(.+)$",
                ln, re.IGNORECASE,
            )
            if m and m.group(1).strip():
                cleaned.append(m.group(1).strip())
                continue
            # Просто "Груз: X".
            if low.startswith("груз:"):
                parts = re.split(r":", ln, maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    cleaned.append(parts[1].strip())
                continue
            # Чисто измерительные строки — пропускаем.
            if re.match(r"^(ед\.\s*изм|кол-во|количес|масс|объ[её]м|нетто|брутто)\b", low):
                continue
            cleaned.append(ln)
        if cleaned:
            joined = " ".join(cleaned[:3])
            joined = joined[:400].strip()
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text:
        for pat in (
            r"наименовани\w*\s+груз\w*\s*[:\-–—]?\s*([^\n\r]{2,400})",
            r"наименовани\w*\s*[:\-–—]\s*([^\n\r]{2,400})",
        ):
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()[:400]
                if candidate and not is_garbage(candidate):
                    return candidate, 0.5

    return MISSING, 0.0


def extract_vehicle(section_body: str, full_text: str) -> Tuple[str, float]:
    """Транспортное средство: ищем сначала ГРЗ (валидированный), затем марку."""
    if section_body:
        # Уберём пробелы внутри возможного ГРЗ перед проверкой: "Р 814 НР 152".
        candidate_text = section_body.upper()
        compact = re.sub(r"(?<=[А-Я0-9])\s+(?=[А-Я0-9])", "", candidate_text)
        grz = find_grz(compact)
        if grz and is_valid_grz(grz):
            # Дополнительная марка/модель, если рядом.
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

        # ГРЗ не нашли — берём первую содержательную непустую строку.
        lines = [ln.strip() for ln in section_body.splitlines() if ln.strip()]
        if lines:
            first = lines[0]
            if not is_garbage(first):
                return first[:80], 0.6

    # ГРЗ где-то в тексте.
    if full_text:
        compact_full = re.sub(r"(?<=[А-ЯA-Z0-9])\s+(?=[А-ЯA-Z0-9])", "", full_text.upper())
        grz = find_grz(compact_full)
        if grz and is_valid_grz(grz):
            return grz, 0.7

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


_RECEPTION_STOP = re.compile(
    r"\n\s*(?:"
    r"\d{1,2}[.)\s]*\s*(?:выдача\s+груз|переадресовк|отметк|стоимость|прочие\s+условия)"
    r"|сдач\w*\s+груз"
    r"|выдач\w*\s+груз"
    r"|доставк\w*\s+груз"
    r"|переадресовк"
    r"|отметк\w*"
    r")",
    re.IGNORECASE,
)


def extract_reception(section_body: str, full_text: str) -> Tuple[str, float]:
    if section_body:
        body = _RECEPTION_STOP.split(section_body, maxsplit=1)[0]
        body = body[:1500].strip()
        if body and not is_garbage(body):
            return body, 0.9

    if full_text:
        m = re.search(r"при[ёе]м\s+груз\w*", full_text, re.IGNORECASE)
        if m:
            chunk = full_text[m.end(): m.end() + 1500]
            chunk = _RECEPTION_STOP.split(chunk, maxsplit=1)[0]
            cleaned = clean_value(chunk, MISSING, GARBAGE)
            if cleaned not in (MISSING, GARBAGE):
                return cleaned, 0.5

    return MISSING, 0.0


# ---------------------------------------------------------------------------


def extract_all(sections: Dict[str, str], full_text: str) -> Dict[str, Tuple[str, float]]:
    """Единая точка: прогоняет все экстракторы и возвращает словарь."""
    head = sections.get("head", "")

    number, c_num, date, c_date = extract_number_and_date(head, full_text)

    shipper, c_shipper = extract_org(
        sections.get("shipper", ""), full_text, "грузоотправитель"
    )
    consignee, c_consignee = extract_org(
        sections.get("consignee", ""), full_text, "грузополучатель"
    )
    carrier, c_carrier = extract_org(
        sections.get("carrier", ""), full_text, "перевозчик"
    )
    cargo, c_cargo = extract_cargo(sections.get("cargo", ""), full_text)
    vehicle, c_vehicle = extract_vehicle(sections.get("vehicle", ""), full_text)
    reception, c_reception = extract_reception(sections.get("reception", ""), full_text)

    return {
        "number": (number, c_num),
        "date": (date, c_date),
        "shipper": (shipper, c_shipper),
        "consignee": (consignee, c_consignee),
        "cargo": (cargo, c_cargo),
        "carrier": (carrier, c_carrier),
        "vehicle": (vehicle, c_vehicle),
        "reception": (reception, c_reception),
    }
