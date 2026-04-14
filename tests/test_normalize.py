# -*- coding: utf-8 -*-
from tn_parser.normalize import (
    clean_value,
    collapse,
    is_garbage,
    normalize_for_sections,
)


def test_soft_hyphen_removed():
    assert normalize_for_sections("при\u00adём") == "приём"


def test_hyphen_break_joined():
    # «при-\nём» → «приём»
    assert normalize_for_sections("при-\nём") == "приём"
    assert normalize_for_sections("транс-\n портное") == "транспортное"


def test_multiple_spaces_collapsed():
    assert normalize_for_sections("a    b\tc") == "a b c"


def test_newlines_preserved():
    text = "строка 1\n\n\nстрока 2"
    assert normalize_for_sections(text) == "строка 1\nстрока 2"


def test_confusables_fixed_in_cyrillic_words():
    # «ИHH» (с латинскими H) → «ИНН»
    assert normalize_for_sections("ИHH 7736207543") == "ИНН 7736207543"
    # «Мapкa» (латинские a, p) → «Марка»
    assert normalize_for_sections("Мapкa: Volvo") == "Марка: Volvo"


def test_latin_only_words_preserved():
    # Чисто латинские слова не трогаем.
    assert "Volvo" in normalize_for_sections("Volvo FH")


def test_mixed_script_product_name_preserved():
    # В «Тенsar» латинский кластер «sar» окружён только латиницей — не трогаем.
    result = normalize_for_sections("Тенsar")
    # Латинская «a» (U+0061) сохранена, а не заменена на кириллическую «а».
    assert "a" in result  # ASCII 'a' (U+0061)
    assert "\u0430" not in result  # cyrillic 'а'


def test_collapse_joins_lines():
    assert collapse("a\nb\nc") == "a b c"


def test_is_garbage():
    assert is_garbage("")
    assert is_garbage("...---???")
    assert not is_garbage("Ромашка")
    assert not is_garbage("12345")


def test_clean_value():
    assert clean_value("", "MISS", "GRB") == "MISS"
    assert clean_value("  Ромашка :,  ", "MISS", "GRB") == "Ромашка"
    assert clean_value("---???", "MISS", "GRB") == "GRB"
