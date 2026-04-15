# -*- coding: utf-8 -*-
"""Резервное извлечение полей через Claude API.

Вызывается из core._build_row() только когда общая уверенность
regex-парсера ниже порога (CONFIDENCE_THRESHOLD).

Требования:
    pip install anthropic>=0.40

Переменная окружения:
    ANTHROPIC_API_KEY — ключ API (https://console.anthropic.com/)
"""

from __future__ import annotations

import os
from typing import Optional

from .models import MISSING, ParsedRow, FieldConfidence

# Импортируем anthropic лениво: если пакет не установлен,
# модуль всё равно загружается, просто fallback недоступен.
try:
    import anthropic
    from pydantic import BaseModel
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

CONFIDENCE_THRESHOLD = 0.5   # ниже → включаем Claude
CLAUDE_MODEL = "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Pydantic-схема — Claude заполняет её поля
# ---------------------------------------------------------------------------

if _ANTHROPIC_AVAILABLE:
    class _WaybillFields(BaseModel):
        number: str
        date: str
        shipper: str
        consignee: str
        cargo: str
        volume: str
        carrier: str
        vehicle: str
        reception: str


# ---------------------------------------------------------------------------
# Системный промпт (кэшируется — платим только при первом вызове в сессии)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Ты — точный парсер транспортных накладных (ТН) на русском языке.
Извлеки поля из OCR-текста ТН и верни их в точности в заданной JSON-схеме.

Правила:
- number: номер ТН (цифры/буквы после «№»). Если не найден — верни "отсутствует".
- date: дата в формате ДД.ММ.ГГГГ. Если не найдена — верни "отсутствует".
- shipper: грузоотправитель — название организации + реквизиты (ИНН и т.п.).
  Не включай служебные фразы типа «является экспедитором», «(реквизиты, позволяющие…)».
- consignee: грузополучатель — аналогично.
- cargo: наименование груза. Убери префиксы «Наименование —», «1.» и т.п.
- volume: масса/объём груза (строка с числом и единицей измерения, напр. «10 000 кг»).
  Если не указан явно — верни "отсутствует".
- carrier: перевозчик (название + ФИО если есть). Убери служебный OCR-мусор.
- vehicle: транспортное средство — марка + ГРЗ в формате «А 123 ВС 777».
  Не включай ИНН вместо ГРЗ.
- reception: место/время приёма груза. Не включай данные следующих разделов
  («Переадресовка», «Выдача груза»).

Если поле явно отсутствует в тексте — возвращай строку "отсутствует".
Не придумывай данные."""


# ---------------------------------------------------------------------------
# Публичная функция
# ---------------------------------------------------------------------------

def claude_enhance(
    row: ParsedRow,
    raw_text: str,
    *,
    api_key: Optional[str] = None,
) -> ParsedRow:
    """Улучшает ParsedRow через Claude API.

    Если anthropic не установлен или ключ не задан — возвращает исходный row.
    Если API вернул ошибку — логирует в row.note и возвращает исходный row.

    Args:
        row:      результат regex-парсера (с низкой уверенностью)
        raw_text: нормализованный OCR-текст ТН (тот же, что шёл в parse_text)
        api_key:  ANTHROPIC_API_KEY; если None — читается из окружения
    """
    if not _ANTHROPIC_AVAILABLE:
        return row

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return row

    client = anthropic.Anthropic(api_key=key)

    try:
        # client.messages.parse() — structured output: Claude заполняет схему
        # cache_control на system — промпт кэшируется на стороне Anthropic,
        # последующие вызовы для других ТН из той же сессии дешевле.
        response = client.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # кэшируем промпт
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"OCR-текст ТН:\n\n{raw_text[:6000]}",  # обрезаем до 6k символов
                }
            ],
            response_format=_WaybillFields,
        )

        f: _WaybillFields = response.parsed

        # Обновляем только поля, которые regex не нашёл (== MISSING)
        enhanced = ParsedRow(
            waybill=row.waybill,
            source=row.source,
            note=row.note + ";CLAUDE_ENHANCED" if row.note else "CLAUDE_ENHANCED",
            confidence=FieldConfidence(
                date=1.0 if f.date != MISSING else 0.0,
                number=1.0 if f.number != MISSING else 0.0,
                shipper=0.9 if f.shipper != MISSING else 0.0,
                consignee=0.9 if f.consignee != MISSING else 0.0,
                cargo=0.9 if f.cargo != MISSING else 0.0,
                carrier=0.9 if f.carrier != MISSING else 0.0,
                vehicle=0.9 if f.vehicle != MISSING else 0.0,
                reception=0.9 if f.reception != MISSING else 0.0,
            ),
            number=f.number,
            date=f.date,
            shipper=f.shipper,
            consignee=f.consignee,
            cargo=f.cargo,
            volume=f.volume,
            carrier=f.carrier,
            vehicle=f.vehicle,
            reception=f.reception,
        )

        # Обновляем waybill с номером
        if enhanced.number not in (MISSING, "неразборчиво"):
            enhanced.waybill = f"Транспортная накладная № {enhanced.number}"

        return enhanced

    except Exception as exc:  # noqa: BLE001
        # Не ломаем обработку — возвращаем исходный row с пометкой
        existing_note = row.note or ""
        row.note = f"{existing_note};CLAUDE_ERR:{exc}".lstrip(";")
        return row
