# -*- coding: utf-8 -*-
"""Извлечение текста из PDF с учётом геометрии блоков.

`fitz.Page.get_text("text")` склеивает двухколоночную вёрстку в кашу — номер
раздела с одной стороны листа оказывается рядом с содержимым совсем другого
раздела. Поэтому берём блоки с координатами, сортируем и склеиваем с учётом
вертикальных разрывов.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

import fitz  # PyMuPDF


# Блок из PyMuPDF: (x0, y0, x1, y1, text, block_no, block_type)
Block = Tuple[float, float, float, float, str, int, int]


def _sort_blocks(blocks: Iterable[Block]) -> List[Block]:
    """Сортировка блоков в порядке чтения.

    Документы ТН часто двухколоночные. Чтобы не склеить колонки, сортируем по
    (y_bucket, x0), где y_bucket округляет координату до ~6 пт — это примерно
    высота строки. Блоки в одной строке тогда группируются слева-направо, а
    разные строки — сверху-вниз.
    """
    BUCKET = 6.0
    return sorted(
        blocks,
        key=lambda b: (round(b[1] / BUCKET), round(b[0])),
    )


def extract_blocks_text(pdf_path: str) -> str:
    """PDF → одна строка с блоками, разделёнными `\\n\\n`.

    Между страницами добавляется маркер "\f" (page break), что полезно для
    дальнейшей сегментации нескольких накладных в одном файле.
    """
    doc = fitz.open(pdf_path)
    try:
        pages_text: List[str] = []
        for page in doc:
            blocks = page.get_text("blocks") or []
            # Отбрасываем графические блоки (block_type == 1) — только текст.
            text_blocks = [b for b in blocks if len(b) >= 7 and b[6] == 0 and b[4]]
            text_blocks = _sort_blocks(text_blocks)
            page_parts = [b[4].strip() for b in text_blocks if b[4].strip()]
            pages_text.append("\n\n".join(page_parts))
        return "\f".join(pages_text)
    finally:
        doc.close()


def extract_plain_text(pdf_path: str) -> str:
    """Упрощённое извлечение текста (без геометрии). Запасной вариант."""
    doc = fitz.open(pdf_path)
    try:
        parts = []
        for page in doc:
            parts.append(page.get_text("text") or "")
        return "\f".join(parts)
    finally:
        doc.close()
