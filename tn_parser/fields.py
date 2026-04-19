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
_NUMBER_AFTER_SYMBOL = re.compile(
    r"(?:№|No\.?)\s*[:\-–—]?\s*"
    r"([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{0,48})",
    re.IGNORECASE,
)
# Запасной: номер вплотную к "№" без пробела («№7145/Б»)
_NUMBER_STICKY = re.compile(
    r"(?:№|No\.?)\s*\n?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{0,48})",
    re.IGNORECASE,
)
# Терпимый к OCR-шуму между «№» и значением: «№ — |7145/Б» (форма с
# разделительной колонкой). \n намеренно НЕ включён — перенос строки
# означает, что значение поля пустое.
_NUMBER_LAX = re.compile(
    r"(?:№|No\.?)[ \t\|:\-–—_.]{0,8}"
    r"([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{1,48})",
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


# Маркеры «после этой строки реквизиты контрагента закончились».
# Нужны как end-anchor в extract_org: любой из этих начальных токенов
# означает, что дальше — подпись/печать/контакт/служебная разметка, и это
# НЕ должно попадать в поле грузоотправителя/грузополучателя/перевозчика.
_ORG_END_MARKERS = re.compile(
    r"^(?:"
    r"подпис[ьиея]"                        # Подпись, Подписью
    r"|м\.?\s*п\.?\b"                      # МП, М.П.
    r"|печат[ьи]\b"
    r"|контактн(?:ое|ый|ого)\s+лиц"        # Контактное лицо
    r"|дата\s+составлен"
    r"|ответственн[оы]й\s+за"
    r"|должност[ьи]\b"
    r"|доверенност"
    r"|ф\.?\s*и\.?\s*о\.?\s+(?:водител|ответствен|предста)"
    r")",
    re.IGNORECASE,
)

# Маркеры «после этой строки наименование груза закончилось». Следующие
# атрибуты (класс опасности, упаковка, тара, способ погрузки, условия
# хранения) — отдельные графы, не часть названия.
_CARGO_END_MARKERS = re.compile(
    r"^(?:"
    r"класс\s+опасност"
    r"|упаковк"
    r"|тара\b"
    r"|способ\s+(?:погрузк|упаковк)"
    r"|условия\s+(?:хранен|перевозк)"
    r"|маркировк"
    r"|номер\s+контейнер"
    r")",
    re.IGNORECASE,
)

# Ключевые слова соседних граф — используется для обрезки fallback-regex:
# когда одно поле и его «сосед» оказались на одной строке OCR.
_ADJACENT_FIELD_CUTOFF = re.compile(
    r"\b(?:"
    r"грузоотправител"
    r"|грузополучател"
    r"|перевозчик"
    r"|транспортн(?:ое|ого)\s+средств"
    r"|при[её]м\s+груз"
    r"|выдач\w*\s+груз"
    r"|переадресовк"
    r"|сопроводительн"
    r"|стоимость\s+(?:услуг|перевозк)"
    r"|оговорк"
    r"|отметк\w+\s+грузо"
    r"|указан\w+\s+грузоотправ"
    r")",
    re.IGNORECASE,
)


# Новый контракт извлечения.
_ORG_PREFIX_RE = re.compile(
    r"(?:"
    r"\b(?:ООО|ОАО|АО|ЗАО|ПАО|НКО|ПБОЮЛ|ИП|ТОО|КФХ|АНО|ЧУ|ФГУП|ГУП|МУП|ФГБУ|ГБУ|НОУ|АНПО)\b"
    # OCR часто пишет «000» (три нуля) вместо «ООО» — опознаём только
    # перед кавычкой или заглавной буквой, чтобы не путать с «000 руб».
    r"|\b000(?=\s*[«\"'“”„А-ЯЁ])"
    r")",
    re.IGNORECASE,
)
_INN_INCLUSIVE_RE = re.compile(r"\bИНН\s*\d{10,12}", re.IGNORECASE)
_FINANCIAL_MARKER_RE = re.compile(
    r"\b(?:ИНН|КПП|ОГРН|ОКПО|ОКВЭД|БИК)\b", re.IGNORECASE
)
_FIO_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\."
    r"|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]+"
)
# OCR регулярно искажает «шт»: «нтт», «иіт», «шт.», «штт» и пр.
# Допускаем 2–3 буквы из множества {ш, н, и, т, i, ї}, последняя — обязательно «т».
_SHT_OCR = r"(?:шт|штт|нтт?|нт|ит|иіт|иiт|штi|штi\.?)"
_CARGO_QTY_SHT_RE = re.compile(
    rf",?\s*\d+(?:[,.]\d+)?\s*{_SHT_OCR}\.?\s*$", re.IGNORECASE
)
_CARGO_KOL_VO_MEST_RE = re.compile(
    r"\bкол[-\s]?во\s+мест\b|\bколичество\s+мест\b", re.IGNORECASE
)
_QTY_SHT_INLINE_RE = re.compile(
    rf"\b(\d+(?:[,.]\d+)?\s*{_SHT_OCR}\.?)", re.IGNORECASE
)
_NETTO_BRUTTO_RE = re.compile(
    r"нетто[^\n]*брутто[^\n]*(?:объ[её]м|м[³3])[^\n]*", re.IGNORECASE
)
_CARGO_ATTR_SPLIT_RE = re.compile(
    r"(?i)\b(?:класс\s+опасност|упаковк|тара\b"
    r"|способ\s+(?:погрузк|упаковк)"
    r"|условия\s+(?:хранен|перевозк)"
    r"|маркировк|номер\s+контейнер)"
)


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


def _cut_at_inn_inclusive(s: str) -> Optional[str]:
    """Если в строке есть «ИНН + 10–12 цифр» — вернуть срез до конца ИНН."""
    m = _INN_INCLUSIVE_RE.search(s)
    if m:
        return s[: m.end()].strip(" ,;")
    return None


def _cut_before_financial(s: str) -> str:
    """Обрезать перед первым ИНН/КПП/ОГРН/ОКПО/ОКВЭД/БИК (НЕ включая)."""
    m = _FINANCIAL_MARKER_RE.search(s)
    if m:
        return s[: m.start()].strip(" ,;")
    return s


def _trim_to_org(s: str) -> str:
    """Отрезать левый префикс до первого ORG-маркера (ООО/АО/ИП/...)."""
    m = _ORG_PREFIX_RE.search(s)
    if m:
        return s[m.start():]
    return s


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
        for rx in (_NUMBER_STICKY, _NUMBER_AFTER_SYMBOL, _NUMBER_LAX):
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
                # Узкое окно (20 симв.) — для коротких маркеров, чтобы
                # «Экземпляр №  Дата 23.07.2022  № 7145/Б» на одной строке
                # не блокировал второй (настоящий) № по слову «экземпляр».
                near_start = max(line_start, m.start() - 20)
                near_ctx = region[near_start: m.start()].lower()
                if "экземпляр" in near_ctx or "экз." in near_ctx:
                    continue
                # Полное начало строки — для длинных заголовков
                # («Приложение No 4», «Постановление Правительства», «Договор No …»).
                line_ctx = region[line_start: m.start()].lower()
                if ("приложение" in line_ctx or "прил." in line_ctx
                        or "постановлен" in line_ctx or "договор" in line_ctx
                        or "к правилам" in line_ctx):
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
            tail = source[anchor.end(): anchor.end() + 500]
            number, conf_num = _pick_number(tail, 0.9 if head else 0.6)
            date_m = _DATE_ANY.search(tail)
            if date_m and is_valid_date(date_m.group(1)):
                date = date_m.group(1)
                conf_date = 1.0 if head else 0.7
            # OCR иногда теряет символ «№» — тогда после «Транспортная
            # накладная» идёт <дата>\n<номер>. Ловим номер как первую
            # осмысленную строку после заголовка, которая не похожа на
            # дату / служебную метку / заголовок раздела.
            if number == MISSING:
                for raw in tail.splitlines()[:10]:
                    ln = raw.strip(" \t|—–-")
                    if not ln or len(ln) < 2 or len(ln) > 50:
                        continue
                    low = ln.lower()
                    if (low.startswith(("транспортн", "заказ", "дата",
                                        "экземпляр", "№", "no", "приложение"))
                            or "накладн" in low):
                        continue
                    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", ln):
                        continue
                    if not re.fullmatch(
                        r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-_/.]{1,48}", ln
                    ):
                        continue
                    if not re.search(r"\d", ln):
                        continue
                    if is_garbage(ln):
                        continue
                    number = ln.strip(" .,:;")
                    conf_num = 0.8 if head else 0.55
                    break

    # Резерв: ищем по всему тексту.
    if number == MISSING and full_text:
        number, c = _pick_number(full_text[:1200], 0.5)
        conf_num = c if number != MISSING else 0.0
    if date == MISSING and full_text:
        for m in _DATE_ANY.finditer(full_text):
            if is_valid_date(m.group(1)):
                date = m.group(1)
                conf_date = 0.5
                break

    return number, conf_num, date, conf_date


# ---------------------------------------------------------------------------


def _collect_org_lines(section_body: str, max_lines: int = 4) -> str:
    """Склеить первые значимые строки контрагента через запятую.

    End-anchors: _ORG_END_MARKERS обрывают, _ADJACENT_FIELD_CUTOFF режет
    строку и прекращает сбор (защита от склейки двухколоночной формы).
    """
    lines = _meaningful_lines(section_body)
    cut_lines: List[str] = []
    for ln in lines:
        if _ORG_END_MARKERS.match(ln):
            break
        cut = _ADJACENT_FIELD_CUTOFF.search(ln)
        if cut and cut.start() > 0:
            head = ln[: cut.start()].strip(" ,;:-–—")
            if head:
                cut_lines.append(head)
            break
        cut_lines.append(ln)
    return ", ".join(ln.rstrip(",") for ln in cut_lines[:max_lines])


def _fallback_org_line(full_text: str, fallback_kw: str) -> Optional[str]:
    pat = re.compile(
        rf"{fallback_kw}\s*[:\-–—]?\s*\n?\s*([^\n\r]{{2,500}})",
        re.IGNORECASE,
    )
    m = pat.search(full_text)
    if not m:
        return None
    candidate = m.group(1).strip()
    cut = _ADJACENT_FIELD_CUTOFF.search(candidate)
    if cut and cut.start() > 0:
        candidate = candidate[: cut.start()].strip(" ,;:-–—")
    if not candidate or _SERVICE_LINE_RE.match(candidate):
        return None
    return candidate


def extract_shipper(section_body: str, full_text: str) -> Tuple[str, float]:
    """Грузоотправитель: от ORG-префикса до «ИНН \\d+» включительно.

    КПП/ОГРН/ОКПО всегда обрезаем. Если ИНН отсутствует — обрезать
    перед первым финансовым маркером.
    """
    if section_body:
        joined = _collect_org_lines(section_body, max_lines=4)
        if joined:
            joined = _trim_to_org(joined)
            cut_inn = _cut_at_inn_inclusive(joined)
            joined = cut_inn if cut_inn else _cut_before_financial(joined)
            joined = joined[:500].strip(" ,;")
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text:
        candidate = _fallback_org_line(full_text, "грузоотправитель")
        if candidate:
            candidate = _trim_to_org(candidate)
            cut_inn = _cut_at_inn_inclusive(candidate)
            candidate = cut_inn if cut_inn else _cut_before_financial(candidate)
            candidate = candidate[:500].strip(" ,;")
            if candidate and not is_garbage(candidate):
                return candidate, 0.5

    return MISSING, 0.0


def extract_consignee(section_body: str, full_text: str) -> Tuple[str, float]:
    """Грузополучатель: ORG-префикс → перед первым ИНН/КПП/ОГРН/ОКПО.

    Получатель всегда без ИНН и КПП.
    """
    if section_body:
        joined = _collect_org_lines(section_body, max_lines=4)
        if joined:
            joined = _trim_to_org(joined)
            joined = _cut_before_financial(joined)
            joined = joined[:500].strip(" ,;")
            if joined and not is_garbage(joined):
                return joined, 0.9

    if full_text:
        candidate = _fallback_org_line(full_text, "грузополучатель")
        if candidate:
            candidate = _trim_to_org(candidate)
            candidate = _cut_before_financial(candidate)
            candidate = candidate[:500].strip(" ,;")
            if candidate and not is_garbage(candidate):
                return candidate, 0.5

    return MISSING, 0.0


# ---------------------------------------------------------------------------


_CARGO_NAME_LINE_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?н.{1,5}мен\w*\s*[:\-–—\u2010-\u2015\u2212]+\s*(.*)$",
    re.IGNORECASE,
)
_CARGO_MEASURE_LINE_RE = re.compile(
    r"^(ед\.\s*изм|кол-во|количес|масс|объ[её]м|нетто|брутто|в том числе)\b",
    re.IGNORECASE,
)


def _clean_cargo_name(name: str) -> str:
    """Очистить наименование груза: без 'кол-во мест', без хвоста 'N шт',
    без класса опасности / упаковки / тары."""
    m_kvm = _CARGO_KOL_VO_MEST_RE.search(name)
    if m_kvm:
        name = name[: m_kvm.start()].strip(" ,;-–—")
    name = _CARGO_QTY_SHT_RE.sub("", name).strip(" ,;-–—")
    parts = _CARGO_ATTR_SPLIT_RE.split(name, maxsplit=1)
    if parts:
        name = parts[0].strip(" ,;-–—")
    return name[:400].strip(" ,;")


def _first_cargo_name_from_lines(lines: List[str]) -> Optional[str]:
    pending_label = False
    for idx, ln in enumerate(lines):
        if _CARGO_END_MARKERS.match(ln):
            break
        m = _CARGO_NAME_LINE_RE.match(ln)
        if m:
            val = m.group(1).strip()
            if val:
                return val
            # «Наименование —» без значения на этой строке: считаем
            # следующую непустую содержательную строку собственно именем.
            pending_label = True
            continue
        low = ln.lower()
        if low.startswith("груз:") or low.startswith("груз —") or low.startswith("груз -"):
            parts = re.split(r"[:\-–—]", ln, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
            continue
        if pending_label:
            if _CARGO_MEASURE_LINE_RE.match(low):
                continue
            if _CARGO_KOL_VO_MEST_RE.search(low):
                continue
            if not re.search(r"[А-Яа-яЁёA-Za-z]{3,}", ln):
                continue
            return ln
    # Не нашли по меткам — первая «содержательная» строка.
    for ln in lines:
        if _CARGO_END_MARKERS.match(ln):
            break
        low = ln.lower()
        if _CARGO_MEASURE_LINE_RE.match(low):
            continue
        if not re.search(r"[А-Яа-яЁёA-Za-z]{3,}", ln):
            continue
        return ln
    return None


def extract_cargo(section_body: str, full_text: str) -> Tuple[str, float]:
    """Наименование груза. Без 'Кол-во мест', без хвоста 'N шт',
    без 'Класс опасности' / 'Упаковка' / 'Тара'."""
    if section_body:
        lines = _meaningful_lines(section_body)
        name = _first_cargo_name_from_lines(lines)
        if name:
            name = _clean_cargo_name(name)
            if name and not is_garbage(name):
                return name, 0.9

    if full_text:
        for pat in (
            r"наименовани\w*\s+груз\w*\s*[:\-–—\u2010-\u2015\u2212]+\s*([^\n\r]{2,400})",
            r"наименовани\w*\s*[:\-–—\u2010-\u2015\u2212]+\s*([^\n\r]{2,400})",
        ):
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                candidate = _clean_cargo_name(m.group(1).strip()[:400])
                if candidate and not is_garbage(candidate):
                    return candidate, 0.5

    return MISSING, 0.0


def extract_volume(cargo_section: str, full_text: str) -> Tuple[str, float]:
    """Объём/количество мест.

    Приоритет: «N шт» внутри наименования → строка «Нетто — X т., Брутто —
    Y т., Объём — Z м³». Иначе MISSING.
    """
    if cargo_section:
        m = _QTY_SHT_INLINE_RE.search(cargo_section)
        if m:
            return m.group(1).strip(), 0.9
        m = _NETTO_BRUTTO_RE.search(cargo_section)
        if m:
            return m.group(0).strip(" ,;"), 0.9
    if full_text:
        m = _QTY_SHT_INLINE_RE.search(full_text)
        if m:
            return m.group(1).strip(), 0.5
        m = _NETTO_BRUTTO_RE.search(full_text)
        if m:
            return m.group(0).strip(" ,;"), 0.5
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


def extract_carrier(section_body: str, full_text: str) -> Tuple[str, float]:
    """Перевозчик: только правая колонка (ФИО).

    Приоритет:
        1) _FIO_RE — «Иванов И.И.» или «И.И. Иванов» → conf 1.0
        2) последняя непустая не-служебная строка → conf 0.7
        3) всё содержимое → conf 0.5
    """
    if section_body:
        m = _FIO_RE.search(section_body)
        if m:
            return m.group(0).strip(), 1.0
        lines = _meaningful_lines(section_body)
        # Если в перевозчике указана организация (ООО/АО/ИП/«000»…) —
        # берём её, обрезая всё после первого финансового маркера.
        org_lines = [ln for ln in lines if _ORG_PREFIX_RE.search(ln)]
        if org_lines:
            target = org_lines[0]
            target = _trim_to_org(target)
            target = _cut_before_financial(target)
            return target[:200].strip(" ,;"), 0.8
        non_fin = [ln for ln in lines if not _FINANCIAL_MARKER_RE.search(ln)]
        if non_fin:
            return non_fin[-1][:200], 0.7
        if lines:
            return lines[-1][:200], 0.7
        stripped = section_body.strip()
        if stripped:
            return stripped[:200], 0.5

    if full_text:
        m = _FIO_RE.search(full_text)
        if m:
            return m.group(0).strip(), 0.5

    return MISSING, 0.0


def _vehicle_marka_candidates(section_body: str, grz: Optional[str]) -> List[str]:
    """Строки, из которых можно вытащить марку. Используем splitlines напрямую,
    чтобы «MAN TGS», «В 404 КМ» не отфильтровались _meaningful_lines."""
    out: List[str] = []
    for raw in section_body.splitlines():
        ln = raw.strip()
        if not ln or _is_service_or_empty(ln):
            continue
        low = ln.lower()
        if re.match(r"^\((тип|марка|модель|регистрационн|рег\.?)", low):
            continue
        if low.startswith(("марка", "модель", "тип", "т/с")):
            parts = re.split(r"[:\-–—]", ln, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                out.append(parts[1].strip())
            continue
        if grz:
            compact_ln = re.sub(
                r"(?<=[А-ЯЁA-Z0-9])\s+(?=[А-ЯЁA-Z0-9])", "", ln.upper()
            )
            m_grz = GRZ_CANDIDATE.search(compact_ln)
            if m_grz:
                if m_grz.start() > 0:
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
                        out.append(brand_raw)
                continue
        if re.search(r"\b(инн|кпп|огрн|окпо)\b", low):
            continue
        if 2 <= len(ln) <= 60:
            out.append(ln)
    return out


def extract_vehicle(section_body: str, full_text: str) -> Tuple[str, float]:
    """Транспортное средство: «МАРКА\\nГРЗ_слитно» (перенос строки внутри ячейки).

    ГРЗ выдаётся без пробелов (С782СК62, не С 782 СК 62).
    """
    if section_body:
        grz = _compact_grz_search(section_body)
        marka_parts = _vehicle_marka_candidates(section_body, grz)

        if grz:
            grz_compact = grz.replace(" ", "")
            if marka_parts:
                marka = marka_parts[0].strip(" ,;")
                return f"{marka}\n{grz_compact}", 1.0
            return grz_compact, 1.0

        lines = _meaningful_lines(section_body)
        if lines:
            return lines[0][:80], 0.5

    if full_text:
        grz = _compact_grz_search(full_text)
        if grz:
            return grz.replace(" ", ""), 0.6
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


def _reception_collect(lines: List[str]) -> Optional[str]:
    """Собирает первую содержательную строку и ещё до 3 следующих,
    пока не появится ИНН / 10–12 цифр подряд (OCR мог потерять «ИНН»).
    """
    if not lines:
        return None
    start = 0
    for i, ln in enumerate(lines):
        if _ORG_PREFIX_RE.search(ln):
            start = i
            break
    collected: List[str] = []
    for ln in lines[start: start + 4]:
        collected.append(ln)
        if _INN_INCLUSIVE_RE.search(ln) or re.search(r"\b\d{10,12}\b", ln):
            break
    return ", ".join(collected)


def _reception_trim(target: str) -> str:
    target = _trim_to_org(target)
    # OCR-огрызок «, Ин,» / «, ИН,» перед 10–12 цифр → восстанавливаем «ИНН ».
    target = re.sub(
        r",\s*[ИИ]н{1,2}\.?\s*,?\s*(?=\d{10,12}\b)",
        ", ИНН ", target, flags=re.IGNORECASE,
    )
    cut_inn = _cut_at_inn_inclusive(target)
    if not cut_inn:
        m = re.search(r"\b\d{10,12}\b", target)
        if m:
            cut_inn = target[: m.end()].strip(" ,;")
    target = cut_inn if cut_inn else _cut_before_financial(target)
    # Огрызки «, Ин» / «, И» / «, Н» в самом конце.
    target = re.sub(r",\s*[А-Яа-яЁёA-Za-z]{1,3}\.?\s*$", "", target)
    return target[:500].strip(" ,;")


def extract_reception(section_body: str, full_text: str) -> Tuple[str, float]:
    """Приём груза: только ПЕРВАЯ содержательная строка.

    От ORG-префикса до «ИНН \\d+» включительно; КПП/ОГРН/ОКПО обрезаются.
    Если ни в одной строке нет ORG-префикса — берём первую как есть.
    """
    if section_body:
        body = _RECEPTION_STOP.split(section_body, maxsplit=1)[0]
        raw = [ln for ln in body.splitlines() if not _looks_noisy(ln)]
        cleaned = [strip_garbage_tokens(ln.strip()) for ln in raw]
        cleaned = [
            ln for ln in cleaned
            if ln and len(ln) > 3 and re.search(
                r"[А-Яа-яЁёA-Za-z]{4,}|\d{5,}|\d{2}\.\d{2}\.\d{4}", ln
            )
        ]
        target = _reception_collect(cleaned)
        if target:
            target = _reception_trim(target)
            if target and not is_garbage(target):
                return target, 0.9

    if full_text:
        m = re.search(r"при[ёе]м\s+груз\w*", full_text, re.IGNORECASE)
        if m:
            chunk = full_text[m.end(): m.end() + 1500]
            chunk = _RECEPTION_STOP.split(chunk, maxsplit=1)[0]
            lines = [ln.strip() for ln in chunk.splitlines() if not _looks_noisy(ln)]
            lines = [ln for ln in lines if ln]
            target = _reception_collect(lines)
            if target:
                target = _reception_trim(target)
                if target and not is_garbage(target):
                    return target, 0.5

    return MISSING, 0.0


# ---------------------------------------------------------------------------


def extract_all(sections: Dict[str, str], full_text: str) -> Dict[str, Tuple[str, float]]:
    """Единая точка: прогоняет все экстракторы и возвращает словарь."""
    head = sections.get("head", "")
    cargo_section = sections.get("cargo", "")

    number, c_num, date, c_date = extract_number_and_date(head, full_text)

    shipper, c_shipper = extract_shipper(sections.get("shipper", ""), full_text)
    consignee, c_consignee = extract_consignee(sections.get("consignee", ""), full_text)
    carrier, c_carrier = extract_carrier(sections.get("carrier", ""), full_text)
    cargo, c_cargo = extract_cargo(cargo_section, full_text)
    volume, c_volume = extract_volume(cargo_section, full_text)
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
