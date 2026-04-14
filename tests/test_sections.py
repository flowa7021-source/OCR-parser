# -*- coding: utf-8 -*-
from pathlib import Path

from tn_parser.normalize import normalize_for_sections
from tn_parser.sections import split_sections


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return normalize_for_sections(
        (FIXTURES / name).read_text(encoding="utf-8")
    )


def test_standard_all_sections():
    text = _load("tn_standard.txt")
    sections = split_sections(text)
    # Шапка
    assert 0 in sections
    assert "Транспортная накладная" in sections[0]
    # Все основные разделы распознаны
    for num in (1, 3, 6, 10, 11):
        assert num in sections, f"Раздел {num} не найден"
    assert "Ромашка" in sections[1]
    assert "Мука" in sections[3]
    assert "Быстрые Перевозки" in sections[10]
    assert "А123ВС777" in sections[11]


def test_messy_recovers_by_name():
    text = _load("tn_messy.txt")
    sections = split_sections(text)
    assert 1 in sections
    assert "Сидоров" in sections[1]
    assert 10 in sections
    assert "ТрансЛайн" in sections[10]
    assert 11 in sections
    # confusables в «Мapкa» починились
    assert "Марка" in sections[11] or "Volvo" in sections[11]


def test_empty_text():
    assert split_sections("") == {}


def test_head_without_markers():
    text = "Просто текст без номеров разделов"
    sections = split_sections(text)
    assert sections == {0: text}
