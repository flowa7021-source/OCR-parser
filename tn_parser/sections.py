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
_ROLE_TITLES: List[Tuple[str, Tuple[str, ...]]] = [
    ("reception", ("прием груза", "приём груза", "погрузка груза")),
    ("consignee", ("грузополучатель",)),
    ("shipper", ("грузоотправитель",)),
    ("vehicle", ("транспортное средство",)),
    ("carrier", ("перевозчик",)),
    ("cargo", ("груз",)),
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


def _find_markers(text: str) -> List[Tuple[int, str]]:
    """Возвращает отсортированный список (offset, role|"__ignored__").

    Стратегия: собираем все возможные маркеры (нумерованные + bare), затем
    для каждой роли оставляем ПЕРВОЕ вхождение. Ignored-маркеры оставляем
    все — они нужны как границы.
    """
    candidates: List[Tuple[int, str]] = []

    # 1) Нумерованные заголовки.
    for m in _NUMBERED.finditer(text):
        role = _classify_title(m.group(2))
        if role is not None:
            candidates.append((m.start(), role))

    # 2) Голые заголовки (на отдельной строке).
    for m in _BARE.finditer(text):
        role = _classify_title(m.group(1))
        if role is not None:
            candidates.append((m.start(), role))

    # Первое вхождение каждой роли.
    seen_roles: set[str] = set()
    markers: List[Tuple[int, str]] = []
    # Сортируем сначала по позиции.
    candidates.sort(key=lambda x: x[0])
    for pos, role in candidates:
        if role == "__ignored__":
            markers.append((pos, role))
            continue
        if role in seen_roles:
            continue
        seen_roles.add(role)
        markers.append((pos, role))

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
        if role not in result:
            result[role] = body

    return result
