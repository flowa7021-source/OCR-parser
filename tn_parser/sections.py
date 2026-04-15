# -*- coding: utf-8 -*-
"""Разбиение текста ТН на разделы по их семантической роли.

Формы ТН бывают разных редакций: нумерация разделов гуляет (например, в
форме из ПП №2200 «Перевозчик» — раздел 10, а в старой форме — раздел 6).
Поэтому вместо ключей-номеров мы возвращаем `Dict[role, content]`.

Ключи:
    "head"      — всё до первого опознанного раздела
    "shipper"   — грузоотправитель
    "consignee" — грузополучатель
    "cargo"     — груз
    "carrier"   — перевозчик
    "vehicle"   — транспортное средство
    "reception" — приём груза
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# Канонические начала заголовков. Более длинные/специфичные — первыми,
# чтобы «Приём груза» не перекрывалось «Груз».
#
# Здесь уживаются три формы ТН:
#   - современная, ПП РФ №2200 от 21.12.2020 (основная);
#   - предыдущая, ПП РФ №272 от 15.04.2011;
#   - «Типовая межотраслевая форма 1-Т» (ТТН), постановление Госкомстата
#     №78 от 28.11.1997 — по-прежнему встречается у консервативных
#     перевозчиков и при перевозке алкогольной/с/х продукции. У неё свои
#     названия разделов («Организация-владелец автотранспорта» вместо
#     «Перевозчик», «Пункт погрузки/разгрузки» вместо «Приём/Выдача груза»).
#     Каждый «1-Т»-синоним помечен комментарием «[1-Т]» — чтобы позже
#     легко понять, что удалять, если от формы откажутся.
# ВАЖНО: выбираем только УНИКАЛЬНЫЕ для формы 1-Т фразы, которые
# одновременно НЕ встречаются как ярлыки внутренних полей в современной ТН.
# Поэтому «Водитель», «Автомобиль» отдельно как заголовки НЕ берём —
# в современной ТН они сплошь попадаются как подписи («Водитель: …»,
# «Марка автомобиля»), и мы случайно разрежем раздел «Приём груза».
# ФИО водителя и ГРЗ авто вытащим обычным путём — по шаблонам, а не по
# заголовку раздела.
_ROLE_TITLES: List[Tuple[str, Tuple[str, ...]]] = [
    ("reception", (
        "прием груза", "приём груза", "погрузка груза",
        "пункт погрузки",                      # [1-Т]
        "погрузочно-разгрузочные операции",    # [1-Т]
    )),
    ("consignee", ("грузополучатель",)),
    ("shipper", ("грузоотправитель",)),
    # "средств" (обрезано) покрывает и «средство», и OCR-варианты вроде
    # «средств6», «средствб» — без хвоста «о».
    ("vehicle", (
        "транспортное средств",
        "марка автомобиля",          # [1-Т] — полный ярлык раздела
        "государственный номерной",  # [1-Т] — «Государственный номерной знак»
    )),
    ("carrier", (
        "перевозчик",
        "организация-владелец автотранспорта",  # [1-Т]
        "организация владелец автотранспорта",  # [1-Т] без дефиса (OCR)
        "владелец автотранспорта",              # [1-Т] усечённый вариант
    )),
    ("cargo", (
        "груз",
        "сведения о грузе",        # [1-Т]
        "товарный раздел",         # [1-Т]
    )),
]

# Заголовки, которые мы узнаём как стоп-маркеры, но контент не забираем.
_IGNORED_TITLES: Tuple[str, ...] = (
    "сопроводительные документы",
    "указания грузоотправителя",
    "условия перевозки",
    "информация о принятии",
    "оговорки и замечания",
    "прочие условия",
    "переадресовк",
    "стоимость услуг",
    "стоимость перевозки",
    "дата составления",
    "отметки",
    "выдача груза",
    "сдача груза",
    # [1-Т] специфические «заключительные» разделы — чисто бухгалтерские,
    # данных о ТН не несут. Служат границами для предыдущих ролей.
    "плательщик",
    "заказчик (плательщик)",
    "пункт разгрузки",
    "транспортный раздел",
    "таксировка",
    "расчёт стоимости",
    "расчет стоимости",
    "прилагаемые документы",
    "итого",
    "всего отпущено",
)


# Заголовок с номером: «1.», «2)», «6 .», нестрогий разделитель.
_NUMBERED = re.compile(
    r"(?m)^\s*(\d{1,2})\s*[.)\u00a0]\s*([А-ЯЁа-яё][^\n]{0,80})"
)

# Заголовок без номера — отдельной строкой.
_BARE = re.compile(
    r"(?mi)^\s*("
    + "|".join(
        re.escape(name)
        for _, names in _ROLE_TITLES
        for name in names
    )
    + r"|"
    + "|".join(re.escape(name) for name in _IGNORED_TITLES)
    + r")\b[^\n]{0,80}$"
)


def _classify_title(title: str) -> Optional[str]:
    """По тексту заголовка определяет роль или "__ignored__"."""
    low = title.lower().strip()
    for ign in _IGNORED_TITLES:
        if low.startswith(ign):
            return "__ignored__"
    for role, names in _ROLE_TITLES:
        for name in names:
            if low.startswith(name):
                return role
    return None


def _find_markers(text: str) -> List[Tuple[int, str, str]]:
    """Возвращает отсортированный список (offset, role|"__ignored__", keyword).

    keyword — канонический «префикс», которым роль была распознана в данном
    месте; нужен потом в split_sections, чтобы отрезать ярлык от значения
    на одной строке (например, «Грузоотправитель ООО …» → «ООО …»).

    Стратегия: собираем все возможные маркеры (нумерованные + bare), затем
    для каждой роли оставляем ПЕРВОЕ вхождение. Ignored-маркеры оставляем
    все — они нужны как границы.
    """
    candidates: List[Tuple[int, str, str]] = []

    def _best_keyword(title: str) -> str:
        """Находит самый длинный из известных префиксов, совпавший с title."""
        low = title.lower().strip()
        best = ""
        for ign in _IGNORED_TITLES:
            if low.startswith(ign) and len(ign) > len(best):
                best = ign
        for _role, names in _ROLE_TITLES:
            for name in names:
                if low.startswith(name) and len(name) > len(best):
                    best = name
        return best

    # 1) Нумерованные заголовки.
    for m in _NUMBERED.finditer(text):
        role = _classify_title(m.group(2))
        if role is not None:
            candidates.append((m.start(), role, _best_keyword(m.group(2))))

    # 2) Голые заголовки (на отдельной строке).
    for m in _BARE.finditer(text):
        role = _classify_title(m.group(1))
        if role is not None:
            candidates.append((m.start(), role, _best_keyword(m.group(1))))

    # Первое вхождение каждой роли.
    seen_roles: set[str] = set()
    markers: List[Tuple[int, str, str]] = []
    # Сортируем сначала по позиции.
    candidates.sort(key=lambda x: x[0])
    for pos, role, kw in candidates:
        if role == "__ignored__":
            markers.append((pos, role, kw))
            continue
        if role in seen_roles:
            continue
        seen_roles.add(role)
        markers.append((pos, role, kw))

    return markers


def split_sections(text: str) -> Dict[str, str]:
    """Делит нормализованный текст на разделы по ролям."""
    if not text:
        return {}

    markers = _find_markers(text)
    result: Dict[str, str] = {}

    if not markers:
        result["head"] = text.strip()
        return result

    head = text[: markers[0][0]].strip()
    if head:
        result["head"] = head

    for i, (pos, role, kw) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        chunk = text[pos:end].strip()

        nl = chunk.find("\n")
        header_line = chunk if nl < 0 else chunk[:nl]
        rest = "" if nl < 0 else chunk[nl + 1 :].strip()

        # Inline-значение в заголовке. Покрываем сразу три формата:
        #   (а) «1. Грузоотправитель: ООО Ромашка»  — двоеточие
        #   (б) «Пункт погрузки   г. Санкт-Петербург»  — 2+ пробелов (форма 1-Т)
        #   (в) «Организация-владелец автотранспорта: ООО …»  — дефис ВНУТРИ
        #       ключевого слова, не разделитель
        # Стратегия: откусываем ровно столько символов, сколько занимает
        # найденный ключ (kw), с возможным числовым префиксом «1.»/«2)».
        # Всё, что осталось, после снятия разделителей — inline.
        inline = ""
        if kw:
            anchor_re = rf"^\s*(?:\d+[.)]\s*)?{re.escape(kw)}"
            m_kw = re.match(anchor_re, header_line, re.IGNORECASE)
            if m_kw:
                tail = header_line[m_kw.end():]
                tail = re.sub(r"^[\s:\-–—]+", "", tail)
                if tail.strip():
                    inline = tail.strip()

        # Если найти по ключу не смогли — пробуем старый разделительный сплит
        # (нужен для «ТРАНСПОРТНАЯ НАКЛАДНАЯ № 123», где kw пустой).
        if not inline:
            inline_split = re.split(r":", header_line, maxsplit=1)
            if len(inline_split) == 2 and inline_split[1].strip():
                inline = inline_split[1].strip()

        if inline and rest:
            body = inline + "\n" + rest
        else:
            body = inline or rest

        if role == "__ignored__":
            continue
        if role not in result:
            result[role] = body

    return result
