# -*- coding: utf-8 -*-
"""Извлечение текста из PDF с сохранением порядка чтения.

Пробуем две стратегии и выбираем ту, что даёт больше содержательного
текста:

    1. `page.get_text("text")` — собственный порядок чтения PDF. Именно эту
       текстовую выдачу получает Acrobat при копировании. Для большинства
       машиночитаемых и OCR-прослоечных PDF это лучший вариант.
    2. `page.get_text("blocks", sort=True)` — блочная выдача с внутренней
       сортировкой PyMuPDF. Иногда даёт более чистый результат для табличных
       форм без нормального порядка чтения.

Результат разделяет страницы символом `\f`.
"""

from __future__ import annotations

from typing import List

import fitz  # PyMuPDF


def _page_text_plain(page) -> str:
    return page.get_text("text") or ""


def _page_text_blocks(page) -> str:
    blocks = page.get_text("blocks", sort=True) or []
    text_blocks = [
        b for b in blocks
        if len(b) >= 7 and b[6] == 0 and b[4] and b[4].strip()
    ]
    return "\n\n".join(b[4].strip() for b in text_blocks)


def _score_text(text: str) -> int:
    """Грубая мера «полезности» текста: количество букв+цифр.

    Нужна, чтобы выбрать из двух стратегий ту, что извлекла больше
    содержательных символов (а не только пробелов и пунктуации).
    """
    return sum(1 for c in text if c.isalnum())


def extract_best_text(pdf_path: str) -> str:
    """Главная функция: выбирает лучшую стратегию страница за страницей."""
    doc = fitz.open(pdf_path)
    try:
        pages: List[str] = []
        for page in doc:
            plain = _page_text_plain(page)
            blocks = _page_text_blocks(page)
            # Предпочитаем plain, но переключаемся на blocks, если он ощутимо
            # богаче (разница > 20%).
            if _score_text(blocks) > _score_text(plain) * 1.2:
                pages.append(blocks)
            else:
                pages.append(plain)
        return "\f".join(pages)
    finally:
        doc.close()


# Сохраняем старые имена для совместимости и для CLI/GUI, где они могут
# явно вызываться как «резерв».
def extract_blocks_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    try:
        return "\f".join(_page_text_blocks(page) for page in doc)
    finally:
        doc.close()


def extract_plain_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    try:
        return "\f".join(_page_text_plain(page) for page in doc)
    finally:
        doc.close()
