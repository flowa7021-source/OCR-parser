# -*- coding: utf-8 -*-
"""Разбиение текста ТН на пронумерованные разделы (1–17).

Российская транспортная накладная (постановление №2200) имеет жёсткую
структуру из 17 пронумерованных разделов. Названия синонимичны у разных
шаблонов, поэтому вместо поиска по названию мы:

    1. Находим маркеры вида `^\\s*(\\d{1,2})[.)]\\s+([А-Я])...` в начале строки.
    2. Дополнительно распознаём альтернативные формулировки («Перевозчик»,
       «Транспортное средство») для тех PDF, где номера не попали в текстовый
       слой.
    3. Склеиваем секцию от маркера до следующего.

API:
    split_sections(normalized_text) -> Dict[int, str]

Ключи — номер раздела (1..17) или 0 для «шапки» до первого маркера.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple


# Канонические названия разделов (для подстраховки, если номер потерян).
SECTION_NAMES: Dict[int, Tuple[str, ...]] = {
    1: ("грузоотправитель",),
    2: ("грузополучатель",),
    3: ("груз",),
    4: ("сопроводительные документы",),
    5: ("указания грузоотправителя",),
    6: ("прием груза", "приём груза"),
    7: ("сдача груза", "выдача груза"),
    8: ("условия перевозки",),
    9: ("информация о принятии",),
    10: ("перевозчик",),
    11: ("транспортное средство",),
    12: ("оговорки и замечания",),
    13: ("прочие условия",),
    14: ("переадресовка",),
    15: ("стоимость услуг", "стоимость перевозки"),
    16: ("дата составления", "подписи сторон"),
    17: ("отметки грузоотправител",),
}


# "1. ", "1)", "1 ." в начале строки + заглавная буква.
_NUMBERED = re.compile(
    r"(?m)^\s*(\d{1,2})[.)\u00a0]\s*([А-ЯЁ][^\n]{0,80})"
)

# Маркер по названию раздела (для повреждённого нумерационного слоя).
_NAMED_HEADERS = [
    (num, re.compile(
        r"(?mi)^\s*(?:\d{1,2}[.)\u00a0]\s*)?(" + "|".join(re.escape(n) for n in names) + r")",
    ))
    for num, names in SECTION_NAMES.items()
]


def _find_markers(text: str) -> List[Tuple[int, int]]:
    """Возвращает список (offset, section_num) в порядке появления."""
    markers: List[Tuple[int, int]] = []
    seen: set[int] = set()

    # 1) Явно пронумерованные заголовки.
    for m in _NUMBERED.finditer(text):
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        if not (1 <= num <= 17):
            continue
        # Доп. проверка: вторая группа похожа на канон названия.
        title = m.group(2).lower()
        canon = SECTION_NAMES.get(num, ())
        if canon and not any(name in title for name in canon):
            # Номер есть, но название другое — игнорируем (чтобы не схватить
            # "1. Введение" из произвольного документа).
            continue
        if num in seen:
            continue
        seen.add(num)
        markers.append((m.start(), num))

    # 2) Маркеры по названию — только для тех номеров, которые ещё не нашли.
    for num, pat in _NAMED_HEADERS:
        if num in seen:
            continue
        m = pat.search(text)
        if m:
            markers.append((m.start(), num))
            seen.add(num)

    markers.sort(key=lambda x: x[0])
    return markers


def split_sections(text: str) -> Dict[int, str]:
    """Делит нормализованный текст на разделы.

    Возвращает словарь {section_num: content}. Ключ 0 — содержимое до первого
    маркера (шапка: «Транспортная накладная № …»).
    Отсутствующие разделы в словарь не попадают.
    """
    if not text:
        return {}

    markers = _find_markers(text)
    result: Dict[int, str] = {}

    if not markers:
        result[0] = text.strip()
        return result

    # Шапка.
    head = text[: markers[0][0]].strip()
    if head:
        result[0] = head

    for i, (pos, num) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        chunk = text[pos:end].strip()
        nl = chunk.find("\n")
        header_line = chunk if nl < 0 else chunk[:nl]
        rest = "" if nl < 0 else chunk[nl + 1 :].strip()

        # Если в первой строке есть `:` после названия раздела — это inline-значение.
        inline = ""
        inline_split = re.split(r"[:\-–—]", header_line, maxsplit=1)
        if len(inline_split) == 2 and inline_split[1].strip():
            inline = inline_split[1].strip()

        if inline and rest:
            body = inline + "\n" + rest
        else:
            body = inline or rest
        result[num] = body

    return result
