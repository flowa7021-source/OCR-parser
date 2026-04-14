# -*- coding: utf-8 -*-
"""Разбиение текста на несколько накладных, если они в одном PDF.

Эвристика: каждое вхождение заголовка «Транспортная накладная» (не как
подпись в разделе 17, а в начале нового документа) считаем границей.
Практический критерий: заголовок + последующая шапка с «№ … от …».
"""

from __future__ import annotations

import re
from typing import List


_DOC_HEADER = re.compile(
    r"(?mi)^\s*транспортн(?:ая|ой)\s+накладн(?:ая|ой)\b"
)


def split_documents(text: str) -> List[str]:
    """Разбивает нормализованный текст на список отдельных накладных.

    Если в тексте один заголовок (или ни одного) — возвращает список из
    одного элемента.
    """
    if not text:
        return []

    matches = list(_DOC_HEADER.finditer(text))
    if len(matches) <= 1:
        return [text.strip()]

    starts = [m.start() for m in matches]
    starts.append(len(text))
    docs: List[str] = []
    for i in range(len(matches)):
        chunk = text[starts[i]:starts[i + 1]].strip()
        # Отфильтровываем слишком короткие фрагменты (ложные срабатывания).
        if len(chunk) >= 80:
            docs.append(chunk)

    # Если после фильтра ничего не осталось — вернём исходный текст.
    return docs if docs else [text.strip()]
