# -*- coding: utf-8 -*-
"""Извлечение конкретных полей из разделов ТН.

Принцип: сначала ищем поле в «своём» разделе (высокая уверенность), если
нет — падаем на эвристики по всему тексту (низкая уверенность). Каждый
экстрактор возвращает кортеж (value, confidence).

Сложности реального OCR:
- В таблице заголовок ячейки и её значение — на разных «логических» строках.
- Ячейки с двумя колонками (способ | ФИО, марка | ГРЗ) после извлечения
  разворачиваются в две соседние строки.
- Пояснения в скобках («(реквизиты, позволяющие идентифицировать…)») и
  формульные подсказки (« является экспедитором», « (а) … ») — мусор.

Экстракторы ниже стараются это учесть.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .normalize import (
    clean_value,
    is_garbage,
    is_noise_line,
    is_ocr_garbage_token,
    strip_garbage_tokens,
)
from .validators import (
    GRZ_CANDIDATE,
    find_grz,
    format_grz,
    is_valid_date,
    is_valid_grz,
)
from .models import MISSING, GARBAGE


_DATE_ANY = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")

# Номер: не захватываем "Экземпляр №" (подпись у графы экземпляра).
# N[º°]? убран — голая латинская «N» слишком широкий маркер (матчит «RENAULT» и т.п.).

# Высокоприоритетный паттерн: табличная ячейка «№ — |7145/Б».
# Стоит первым — даёт правильный номер даже когда в заголовке ТН
# OCR прочитал «7145/6» вместо «7145/Б».
_NUMBER_LABELED_FIELD = re.compile(
    r"^№\s*[\-–—]\s*\|?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{0,48})",
    re.IGNORECASE | re.MULTILINE,
)
_NUMBER_AFTER_SYMBOL = re.compile(
    r"(?:№|No\.?|N[°º])\s*[:\-–—]?\s*\|?\s*"   # \|? — разделитель ячейки таблицы
    r"([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{0,48})",
    re.IGNORECASE,
)
# Запасной: номер вплотную к "№" без пробела («№7145/Б»)
_NUMBER_STICKY = re.compile(
    r"(?:№|No\.?|N[°º])\s*\n?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{0,48})",
    re.IGNORECASE,
)

_WAYBILL_HEADER = re.compile(
    r"транспортн(?:ая|ой)\s+накладн(?:ая|ой)", re.IGNORECASE
)

# Строки-служебки, которые надо пропустить в начале раздела контрагента.
_SERVICE_LINE_RE = re.compile(
    r"^(?:"
    r"является\s+(?:экспедитором|грузоотправителем)"
    r"|\(\s*реквизиты\b"
    r"|полное\s+наименование"
    r"|сокращ\w*\s+наименование"
    r"|наименование\s+(?:юр|лица)"
    r"|фио\b|ф\.?и\.?о\.?"
    r"|экземпляр\s*№?"
    r"|реквизиты\s+документа"
    r"|да\b|нет\b"
    r"|заказчик\s+услуг\b"                 # «Заказчик услуг по организации…»
    r"|при\s+наличи[ии]\b"                 # автономная пометка «(при наличии)»
    r"|перевозки\s+груза\b"               # продолжение аннотации на отдельной строке
    r"|[\-–—]\s+\("                       # «— (реквизиты…)» — аннотация водителя
    r"|[\[\(][\s xх×✓✔][\]\)]"   # чекбоксы
    r"|[\-–—=_\s]{3,}"            # разделители из дефисов
    r"|\([а-яА-Я]+\)"             # короткие пометки в скобках: «(а)», «(б)»
    r")",
    re.IGNORECASE,
)


def _is_service_or_empty(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # Целиком в скобках: «(реквизиты, позволяющие…)»
    if re.match(r"^\(.+\)\s*$", s):
        return True
    if _SERVICE_LINE_RE.match(s):
        return True
    return False


def _meaningful_lines(body: str) -> List[str]:
    """Разбиваем тело секции на строки, выкидываем служебные."""
    if not body:
        return []
    out = []
    for raw in body.splitlines():
        line = raw.strip()
        if _is_service_or_empty(line):
            continue
        # Строки с инструкцией-пояснением в стиле «(…текст…)» после значения:
        # «Самовывоз  (реквизиты, позволяющие…)» — чистим хвост.
        line = re.sub(r"\s*\([^)]*реквизиты[^)]*\)\s*$", "", line, flags=re.IGNORECASE)
        # Чистим «Заказчик услуг … (при наличии)» — он может прилипнуть
        # к строке с именем организации как левый префикс:
        # «Га Заказчик услуг по организации, перевозки груза (при наличии), ООО …»
        # Реальный OCR часто ставит запятую внутри фразы, поэтому [^,]* недостаточно.
        # Решение: при наличии «заказчик услуг» на строке — ищем первую org-метку
        # (ООО/АО/ИП…) и берём текст начиная с неё; если org-метки нет — вся строка
        # является служебной пометкой и отбрасывается.
        if re.search(r"заказчик\s+услуг", line, re.IGNORECASE):
            m_org = re.search(r"\b(ООО|АО|ЗАО|ПАО|ИП|ПБОЮЛ)\b", line, re.IGNORECASE)
            if m_org:
                line = line[m_org.start():]
            else:
                continue
        line = line.strip(" \t,;")
        # Слишком короткие строки — почти наверняка OCR-мусор (одиночные
        # символы/слоги): «Г», «а.», «ГЕР», «ав4'». Значимых данных не несут.
        if len(line) <= 3:
            continue
        # Строка без единого содержательного слова (≥ 4 букв подряд), даты или
        # длинного числа (ИНН/индекс) — OCR-мусор вроде «/ Й /», «11 [3] Т».
        if (not re.search(r"[А-Яа-яЁёA-Za-z]{4,}", line)
                and not re.search(r"\d{2}\.\d{2}\.\d{4}", line)
                and not re.search(r"\d{5,}", line)):
            continue
        # Строка-шум по токен-статистике: большая доля мусорных токенов
        # («іі-і», «Ц:і», «Бекам'тбд»).
        if is_noise_line(line):
            continue
        # Чистим одиночные мусорные токены в строке (украинские буквы,
        # апострофы-в-середине, 3+ переключения скриптов).
        line = strip_garbage_tokens(line)
        line = line.strip(" \t,;")
        if not line or len(line) <= 3:
            continue
        if not is_garbage(line):
            out.append(line)
    return out


# ---------------------------------------------------------------------------


def extract_number_and_date(
    head: str, full_text: str
) -> Tuple[str, float, str, float]:
    """Возвращает (number, conf_number, date, conf_date).

    Поиск номера:
        1) После «Транспортная накладная» ищем № + значение в 400 символах.
        2) Если не вышло — сканируем весь текст, пропуская «Экземпляр №».
    """
    number = MISSING
    conf_num = 0.0
    date = MISSING
    conf_date = 0.0

    def _pick_number(region: str, base_conf: float) -> Tuple[str, float]:
        # Собираем все кандидаты и выбираем первый непустой/осмысленный.
        # _NUMBER_LABELED_FIELD идёт первым: он находит «№ — |7145/Б» в
        # табличной ячейке и возвращает правильное значение даже когда OCR
        # прочитал заголовок как «НАКЛАДНАЯ №7145/6» (6 вместо Б).
        for rx in (_NUMBER_LABELED_FIELD, _NUMBER_STICKY, _NUMBER_AFTER_SYMBOL):
            for m in rx.finditer(region):
                # Проверяем, не «Экземпляр №» ли это.
                # Стратегия: смотрим на ближайшие 20 символов слева, но НЕ
                # пересекаем границу строки. Это обрабатывает два случая:
                #   а) «Экземпляр №\n№ 7145/Б» — переносы между строками:
                #      second № не видит «экземпляр» из предыдущей строки.
                #   б) «Экземпляр №  Дата 23.07.2022  № 7145/Б» — одна строка:
                #      second № смотрит лишь 20 симв. назад → «23.07.2022  »,
                #      «экземпляр» не попадает в окно.
                line_start = region.rfind("\n", 0, m.start())
                line_start = 0 if line_start < 0 else line_start + 1
                near_start = max(line_start, m.start() - 20)
                near_ctx = region[near_start: m.start()].lower()
                if "экземпляр" in near_ctx or "экз." in near_ctx:
                    continue
                candidate = m.group(1).strip(" .,:;")
                if not candidate:
                    continue
                if candidate.lower() in ("экземпляр", "экз"):
                    continue
                if is_garbage(candidate):
                    continue
                return candidate, base_conf
        return MISSING, 0.0

    source = head if head else full_text
    if source:
        anchor = _WAYBILL_HEADER.search(source)
        if anchor:
            tail = source[anchor.end(): anchor.end() + 800]
            number, conf_num = _pick_number(tail, 0.9 if head else 0.6)
            date_m = _DATE_ANY.search(tail)
            if date_m and is_valid_date(date_m.group(1)):
                date = date_m.group(1)
                conf_date = 1.0 if head else 0.7

    # Резерв: ищем по всему тексту.
    if number == MISSING and full_text:
        number, c = _pick_number(full_text[:2000], 0.5)
        conf_num = c if number != MISSING else 0.0
    if date == MISSING and full_text:
        for m in _DATE_ANY.finditer(full_text):
            if is_valid_date(m.group(1)):
                date = m.group(1)
                conf_date = 0.5
                break

    return number, conf_num, date, conf_date


# ---------------------------------------------------------------------------


def extract_org(
    section_body: str,
    full_text: str,
    fallback_kw: str,
    max_lines: int = 4,
    max_len: int = 500,
) -> Tuple[str, float]:
    """Извлекает реквизиты контрагента из раздела.

    Берём до max_lines непустых не-служебных строк и склеиваем через «, ».
    Это даёт «ООО …, адрес, ИНН …, КПП …». Для карьера в табличной форме
    также ловим «Самовывоз, Рябов В.К.» — обе колонки в одной ячейке.
    """
    if section_body:
        lines = _meaningful_lines(section_body)
        if lines:
            joined = ", ".join(ln.rstrip(",") for ln in lines[:max_lines])
            joined = joined[:max_len].strip(" ,;")
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text and fallback_kw:
        pat = re.compile(
            rf"{fallback_kw}\s*[:\-–—]?\s*\n?\s*([^\n\r]{{2,350}})",
            re.IGNORECASE,
        )
        m = pat.search(full_text)
        if m:
            candidate = m.group(1).strip()
            if (candidate and not is_garbage(candidate)
                    and not _SERVICE_LINE_RE.match(candidate)):
                return candidate, 0.5

    return MISSING, 0.0


# ---------------------------------------------------------------------------


def extract_cargo(section_body: str, full_text: str) -> Tuple[str, float]:
    """Извлекает наименование груза, снимая префикс «Наименование —»."""
    if section_body:
        lines = _meaningful_lines(section_body)
        cleaned: List[str] = []
        for ln in lines:
            low = ln.lower()

            # Строка-заголовок без значения: «1. Нанменование —» (OCR разбил
            # на отдельную строку, значение идёт следующей строкой).
            # Пропускаем её целиком — значение возьмём из следующей строки.
            if re.match(
                r"^\s*(?:\d+[.)]\s*)?н.{1,5}мен\w*\s*[:\-–—\u2010-\u2015\u2212]*\s*$",
                ln, re.IGNORECASE,
            ):
                continue

            # "1. Наименование — Блок облицовочный…" → «Блок облицовочный…»
            # Допускаем OCR-варианты: «Нанменование», «Наиименование» и т.п.:
            # н + 1–5 произвольных символов + «мен» + хвост.
            m = re.match(
                r"^\s*(?:\d+[.)]\s*)?н.{1,5}мен\w*\s*[:\-–—\u2010-\u2015\u2212]+\s*(.+)$",
                ln, re.IGNORECASE,
            )
            if m and m.group(1).strip():
                cleaned.append(m.group(1).strip())
                continue
            # "Груз: X"
            if low.startswith("груз:") or low.startswith("груз —"):
                parts = re.split(r"[:\-–—]", ln, maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    cleaned.append(parts[1].strip())
                continue
            # Пропускаем чисто измерительные строки.
            if re.match(
                r"^(ед\.\s*изм|кол-во|количес|масс|объ[её]м|нетто|брутто|в том числе)\b",
                low,
            ):
                continue
            # Пропускаем строки без единой буквы (только числа и знаки —
            # обычно «20,52 т., 20,835 т., 8,73 м³»).
            if not re.search(r"[А-Яа-яЁёA-Za-z]{3,}", ln):
                continue
            cleaned.append(ln)

        if cleaned:
            joined = " ".join(cleaned[:3])
            joined = joined[:400].strip(" ,;")
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text:
        for pat in (
            r"наименовани\w*\s+груз\w*\s*[:\-–—\u2010-\u2015\u2212]+\s*([^\n\r]{2,400})",
            r"наименовани\w*\s*[:\-–—\u2010-\u2015\u2212]+\s*([^\n\r]{2,400})",
        ):
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()[:400]
                if candidate and not is_garbage(candidate):
                    return candidate, 0.5

    return MISSING, 0.0


_VOLUME_KW = re.compile(r"\b(нетто|брутто|объ[её]м|масс[аы]|вес\b)", re.IGNORECASE)
_VOLUME_NUM = re.compile(r"\d")


def extract_volume(section_body: str, full_text: str) -> Tuple[str, float]:
    """Извлекает строку с весом/объёмом груза (Нетто/Брутто/Объём)."""
    if section_body:
        # OCR часто бьёт «Нетто — 20,52 т., Брутто — 20,835 т.» на несколько
        # строк, где каждая заканчивается на «—» (незакрытое тире).
        # Склеиваем такие строки в одну перед поиском.
        raw_lines = section_body.splitlines()
        joined: List[str] = []
        i = 0
        while i < len(raw_lines):
            s = raw_lines[i].rstrip()
            while re.search(r"[–—]\s*$", s) and i + 1 < len(raw_lines):
                i += 1
                nxt = raw_lines[i].strip()
                if nxt:
                    s = s.rstrip() + " " + nxt
            joined.append(s.strip())
            i += 1

        for s in joined:
            if _VOLUME_KW.search(s) and _VOLUME_NUM.search(s):
                if not is_garbage(s):
                    return s, 0.9
    if full_text:
        for m in _VOLUME_KW.finditer(full_text):
            ls = full_text.rfind("\n", 0, m.start())
            ls = 0 if ls < 0 else ls + 1
            le = full_text.find("\n", m.end())
            le = len(full_text) if le < 0 else le
            ln = full_text[ls:le].strip()
            if _VOLUME_NUM.search(ln) and not is_garbage(ln):
                return ln, 0.5
    return MISSING, 0.0


# ---------------------------------------------------------------------------


def _compact_grz_search(region: str) -> Optional[str]:
    """Схлопываем пробелы внутри буквенно-цифровых кластеров и ищем ГРЗ."""
    compact = re.sub(
        r"(?<=[А-ЯЁA-Z0-9])\s+(?=[А-ЯЁA-Z0-9])", "", region.upper()
    )
    grz = find_grz(compact)
    if grz and is_valid_grz(grz):
        return grz
    return None


def extract_vehicle(section_body: str, full_text: str) -> Tuple[str, float]:
    """Транспортное средство: марка + ГРЗ в одной ячейке.

    Формат вывода — «RENAULT Р 814 НР 152»: сначала марка (если есть),
    потом канонический ГРЗ. Если марки нет — только ГРЗ.
    """
    if section_body:
        grz = _compact_grz_search(section_body)

        # Строки без служебки и без "(тип, марка …)"-подсказок.
        lines = _meaningful_lines(section_body)

        # Фильтруем подсказки-пометки вроде «(тип, марка, грузоподъемность…)».
        def _is_hint(ln: str) -> bool:
            low = ln.lower()
            return bool(re.match(r"^\((тип|марка|модель|регистрационн|рег\.?)", low))

        lines = [ln for ln in lines if not _is_hint(ln)]

        # Разделяем строки с меткой «Марка:», «Модель:» и без.
        marka_parts: List[str] = []
        for ln in lines:
            low = ln.lower()
            if low.startswith(("марка", "модель", "тип", "т/с")):
                parts = re.split(r"[:\-–—]", ln, maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    marka_parts.append(parts[1].strip())
                continue
            # Строка, в которой есть ГРЗ.
            if grz:
                compact_ln = re.sub(
                    r"(?<=[А-ЯЁA-Z0-9])\s+(?=[А-ЯЁA-Z0-9])", "", ln.upper()
                )
                m_grz = GRZ_CANDIDATE.search(compact_ln)
                if m_grz:
                    # Если найденный кандидат совпадает с нашим ГРЗ — эта строка
                    # содержит ГРЗ. Пробуем вытащить марку из префикса до ГРЗ.
                    if m_grz.start() > 0:
                        # Считаем непробельные символы в оригинальной строке:
                        # ищем позицию, где их накопилось m_grz.start() штук.
                        ns = 0
                        end_pos = len(ln)
                        for ci, ch in enumerate(ln):
                            if not ch.isspace():
                                if ns == m_grz.start():
                                    end_pos = ci
                                    break
                                ns += 1
                        brand_raw = ln[:end_pos].strip(" ,;()")
                        if brand_raw and len(brand_raw) >= 2:
                            marka_parts.append(brand_raw)
                    continue
            # Строки с инн/кпп — точно не ТС.
            if re.search(r"\b(инн|кпп|огрн|окпо)\b", low):
                continue
            # Похоже на название марки (буквы/цифры, короткая).
            if 2 <= len(ln) <= 60:
                marka_parts.append(ln)

        if grz:
            pretty = format_grz(grz)
            if marka_parts:
                # Берём ПЕРВУЮ содержательную часть (обычно марка в верхней строке).
                marka = marka_parts[0].strip(" ,;")
                return f"{marka} {pretty}", 1.0
            return pretty, 1.0

        # ГРЗ не нашли, но раздел есть — отдаём первую содержательную строку.
        if lines:
            return lines[0][:80], 0.5

    # Фоллбэк — ищем ГРЗ во всём тексте.
    if full_text:
        grz = _compact_grz_search(full_text)
        if grz:
            return format_grz(grz), 0.6
        for pat in (
            r"гос\.?\s*номер\s*[:\-–—]?\s*([^\n\r]{2,40})",
            r"рег\.?\s*знак\s*[:\-–—]?\s*([^\n\r]{2,40})",
            r"государственн\w*\s+регистрационн\w*\s+номер\s*[:\-–—]?\s*([^\n\r]{2,40})",
        ):
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                candidate = clean_value(m.group(1), MISSING, GARBAGE)
                if candidate not in (MISSING, GARBAGE):
                    return candidate, 0.4

    return MISSING, 0.0


# ---------------------------------------------------------------------------


_RECEPTION_STOP = re.compile(
    r"\n\s*(?:"
    r"\d{1,2}\s*[.)]\s*(?:выдача\s+груз|переадресовк|отметк|стоимость|прочие\s+условия)"
    r"|сдач\w*\s+груз"
    r"|выдач\w*\s+груз"
    r"|доставк\w*\s+груз"
    r"|переадресовк"
    r"|отметк\w+\s+грузоотпр"
    r")",
    re.IGNORECASE,
)

# Строка считается мусорной, если <30% символов — буквы/цифры, и в ней
# много «технических» символов (скобки/слэши/подчёркивания, длинные серии
# одиночных пробелов).
_RECEPTION_NOISE_CHARS = set("[](){}|\\/=_~^`<>*#$%")


def _looks_noisy(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # Строки ≤ 3 символов — одиночные литеры/слоги вроде «Г», «а.», «ГЕР».
    if len(s) <= 3:
        return True
    # Аннотации-пояснения в скобках: «(адрес места погрузки)»,
    # «(заявленные дата и время подачи транспортного средства…)» и т.п.
    # Они пропускаются, т.к. содержат реальные русские слова, но нам не нужны.
    if re.match(r"^\s*\(.+\)\s*$", s):
        return True
    # Украинские буквы/диакритика/встроенные апострофы по токенам.
    if is_noise_line(s):
        return True
    # Нет ни одного «осмысленного» слова (≥ 4 букв подряд), ни даты, ни
    # длинного числа (телефон/ИНН/индекс)? Значит строка — OCR-мусор вроде
    # «11 [3] Т» или «000 д».
    if (not re.search(r"[А-Яа-яЁёA-Za-z]{4,}", s)
            and not re.search(r"\d{2}\.\d{2}\.\d{4}", s)
            and not re.search(r"\d{5,}", s)):
        return True
    alnum = sum(1 for c in s if c.isalnum())
    if alnum == 0:
        return True
    if len(s) >= 4 and alnum / len(s) < 0.3:
        return True
    noise = sum(1 for c in s if c in _RECEPTION_NOISE_CHARS)
    if noise >= 3 and noise / max(len(s), 1) > 0.3:
        return True
    # Подряд идущие короткие «токены» из 1-2 символов — это почти всегда
    # OCR-мусор на границе ячеек таблицы.
    tokens = s.split()
    if len(tokens) >= 4:
        short = sum(1 for t in tokens if len(t) <= 2)
        if short / len(tokens) > 0.6:
            return True
    return False


def extract_reception(section_body: str, full_text: str) -> Tuple[str, float]:
    """Приём груза с агрессивной чисткой OCR-шума."""
    if section_body:
        body = _RECEPTION_STOP.split(section_body, maxsplit=1)[0]
        lines = [ln for ln in body.splitlines() if not _looks_noisy(ln)]
        # В выживших строках ещё раз выпиливаем одиночные мусорные токены.
        lines = [strip_garbage_tokens(ln.strip()) for ln in lines]
        # После стрипинга строка может потерять все осмысленные слова — как
        # «000 д» после удаления «"Бекам'тбд». Такие остатки — шум.
        lines = [
            ln for ln in lines
            if ln and len(ln) > 3 and re.search(
                r"[А-Яа-яЁёA-Za-z]{4,}|\d{5,}|\d{2}\.\d{2}\.\d{4}", ln
            )
        ]
        # Отбрасываем повторный блок с реквизитами грузоотправителя, если
        # он идёт ВТОРЫМ (такое бывает, когда «Приём груза» копирует контент
        # из раздела 1 — нам это неинтересно, у нас уже есть shipper).
        if lines:
            body_clean = "\n".join(lines[:12])[:1200].strip()
            if body_clean and not is_garbage(body_clean):
                return body_clean, 0.9

    if full_text:
        m = re.search(r"при[ёе]м\s+груз\w*", full_text, re.IGNORECASE)
        if m:
            chunk = full_text[m.end(): m.end() + 1500]
            chunk = _RECEPTION_STOP.split(chunk, maxsplit=1)[0]
            lines = [ln for ln in chunk.splitlines() if not _looks_noisy(ln)]
            cleaned = "\n".join(ln.strip() for ln in lines if ln.strip())[:1200]
            cleaned = clean_value(cleaned, MISSING, GARBAGE)
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
        sections.get("carrier", ""), full_text, "перевозчик",
        max_lines=3, max_len=300,
    )
    cargo, c_cargo = extract_cargo(sections.get("cargo", ""), full_text)
    volume, c_volume = extract_volume(sections.get("cargo", ""), full_text)
    vehicle, c_vehicle = extract_vehicle(sections.get("vehicle", ""), full_text)
    reception, c_reception = extract_reception(sections.get("reception", ""), full_text)

    return {
        "number": (number, c_num),
        "date": (date, c_date),
        "shipper": (shipper, c_shipper),
        "consignee": (consignee, c_consignee),
        "cargo": (cargo, c_cargo),
        "volume": (volume, c_volume),
        "carrier": (carrier, c_carrier),
        "vehicle": (vehicle, c_vehicle),
        "reception": (reception, c_reception),
    }
