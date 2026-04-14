# -*- coding: utf-8 -*-
"""Разбиение текста ТН на разделы по их семантической роли.

Формы ТН бывают разных редакций: нумерация разделов гуляет (например, в
форме из ПП №2200 «Перевозчик» — раздел 10, а в старой форме — раздел 6).
Поэтому вместо ключей-номеров мы возвращаем `Dict[role, content]`, где
role — это одна из фиксированных строк:

    "head"      — всё, что до первого опознанного раздела (шапка)
    "shipper"   — грузоотправитель
    "consignee" — грузополучатель
    "cargo"     — груз
    "carrier"   — перевозчик
    "vehicle"   — транспортное средство
    "reception" — приём груза

Разделы, которые нам не нужны (сопроводительные документы, переадресовка,
отметки, стоимость) просто игнорируются.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# Канонические начала заголовков. Порядок важен: более длинные (и потому более
# специфичные) идут первыми, чтобы «Приём груза» не перекрывалось «Груз».
_ROLE_TITLES: List[Tuple[str, Tuple[str, ...]]] = [
    ("reception", ("прием груза", "приём груза", "погрузка груза")),
    ("consignee", ("грузополучатель",)),
    ("shipper", ("грузоотправитель",)),
    ("vehicle", ("транспортное средство",)),
    ("carrier", ("перевозчик",)),
    ("cargo", ("груз",)),  # самое короткое — последнее
]

# Заголовки, которые мы узнаём, но контент нам не нужен. Главное — использовать
# их как стоп-маркеры (границы следующего раздела).
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
)


# "1. ", "1)", "1 ." — в начале строки, плюс заглавная буква.
_NUMBERED = re.compile(r"(?m)^\s*(\d{1,2})[.)\u00a0]\s*([А-ЯЁ][^\n]{0,80})")

# Заголовок без номера (на случай повреждённого текстового слоя).
_BARE_TITLE_RE = re.compile(
    r"(?mi)^\s*("
    + "|".join(
        re.escape(name)
        for _, names in _ROLE_TITLES
        for name in names
    )
    + r")\b[^\n]{0,80}$"
)


def _classify_title(title: str) -> Optional[str]:
    """По тексту заголовка раздела определяет роль (или None)."""
    low = title.lower().strip()
    # Отбрасываем явно игнорируемые.
    for ign in _IGNORED_TITLES:
        if low.startswith(ign):
            return "__ignored__"
    for role, names in _ROLE_TITLES:
        for name in names:
            if low.startswith(name):
                return role
    return None


def _find_markers(text: str) -> List[Tuple[int, str]]:
    """Возвращает список (offset, role | "__ignored__") в порядке появления.

    Игнорируемые разделы тоже участвуют — они нужны как границы между
    интересующими нас.
    """
    markers: List[Tuple[int, str]] = []
    seen_roles: set[str] = set()

    # 1) Пронумерованные заголовки.
    for m in _NUMBERED.finditer(text):
        title = m.group(2)
        role = _classify_title(title)
        if role is None:
            continue
        if role != "__ignored__" and role in seen_roles:
            continue
        if role != "__ignored__":
            seen_roles.add(role)
        markers.append((m.start(), role))

    # 2) Заголовки без номера (если соответствующей роли ещё не нашли).
    if not all(r in seen_roles for r, _ in _ROLE_TITLES):
        for m in _BARE_TITLE_RE.finditer(text):
            role = _classify_title(m.group(1))
            if role is None or role == "__ignored__":
                continue
            if role in seen_roles:
                continue
            seen_roles.add(role)
            markers.append((m.start(), role))

    markers.sort(key=lambda x: x[0])
    return markers


def split_sections(text: str) -> Dict[str, str]:
    """Делит нормализованный текст на разделы по их семантической роли.

    Возвращает словарь {role: content}. Ключ «head» — всё до первого маркера.
    Игнорируемые разделы в словарь не попадают, но используются как границы.
    """
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

    for i, (pos, role) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        chunk = text[pos:end].strip()

        nl = chunk.find("\n")
        header_line = chunk if nl < 0 else chunk[:nl]
        rest = "" if nl < 0 else chunk[nl + 1 :].strip()

        # Inline-значение в заголовке: "1. Грузоотправитель: ООО Ромашка".
        inline = ""
        inline_split = re.split(r"[:\-–—]", header_line, maxsplit=1)
        if len(inline_split) == 2 and inline_split[1].strip():
            inline = inline_split[1].strip()

        if inline and rest:
            body = inline + "\n" + rest
        else:
            body = inline or rest

        if role == "__ignored__":
            continue
        # Первая встреченная секция роли побеждает.
        if role not in result:
            result[role] = body

    return result
