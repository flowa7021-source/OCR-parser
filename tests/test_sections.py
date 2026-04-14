# -*- coding: utf-8 -*-
from pathlib import Path

from tn_parser.normalize import normalize_for_sections
from tn_parser.sections import split_sections


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return normalize_for_sections(
        (FIXTURES / name).read_text(encoding="utf-8")
    )


def test_standard_all_roles():
    sec = split_sections(_load("tn_standard.txt"))
    assert "head" in sec and "Транспортная накладная" in sec["head"]
    for role in ("shipper", "consignee", "cargo", "carrier", "vehicle", "reception"):
        assert role in sec, f"Роль {role} не найдена"
    assert "Ромашка" in sec["shipper"]
    assert "Василёк" in sec["consignee"]
    assert "Мука" in sec["cargo"]
    assert "Быстрые Перевозки" in sec["carrier"]
    assert "А123ВС777" in sec["vehicle"]


def test_real_sample_numbering_variant():
    """Старая редакция ТН: перевозчик = раздел 6, ТС = 7, приём = 8."""
    sec = split_sections(_load("tn_real_7145B.txt"))
    assert "Бекам" in sec["shipper"]
    assert "Моспроект" in sec["consignee"]
    assert "Блок облицовочный" in sec["cargo"] or "Наименование" in sec["cargo"]
    assert "Самовывоз" in sec["carrier"]
    assert "RENAULT" in sec["vehicle"] or "Р 814" in sec["vehicle"]
    assert "Подолино" in sec["reception"]
    # Следующие разделы не должны утекать в приём груза.
    assert "Переадресовка" not in sec["reception"]


def test_messy_recovers_by_name():
    sec = split_sections(_load("tn_messy.txt"))
    assert "Сидоров" in sec["shipper"]
    assert "ТрансЛайн" in sec["carrier"]
    assert "vehicle" in sec


def test_empty_text():
    assert split_sections("") == {}


def test_head_without_markers():
    text = "Просто текст без номеров разделов"
    sec = split_sections(text)
    assert sec == {"head": text}
