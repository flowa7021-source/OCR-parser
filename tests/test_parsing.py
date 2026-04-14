# -*- coding: utf-8 -*-
"""Интеграционные тесты парсинга на текстовых фикстурах."""

from pathlib import Path

from tn_parser import MISSING, parse_text
from tn_parser.normalize import normalize_for_sections


FIXTURES = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return normalize_for_sections((FIXTURES / name).read_text(encoding="utf-8"))


class TestStandardWaybill:
    def setup_method(self) -> None:
        rows = parse_text(_text("tn_standard.txt"), "tn_standard.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_number(self):
        assert self.row.number == "ТН-2024/00127"
        assert self.row.waybill == "Транспортная накладная № ТН-2024/00127"

    def test_date(self):
        assert self.row.date == "15.03.2024"

    def test_shipper(self):
        assert "Ромашка" in self.row.shipper
        # Терминатор обрезал всё после «ИНН».
        assert "ИНН" not in self.row.shipper
        assert "Ленина" not in self.row.shipper

    def test_cargo(self):
        assert "Мука" in self.row.cargo

    def test_carrier(self):
        assert "Быстрые Перевозки" in self.row.carrier
        assert "ИНН" not in self.row.carrier

    def test_vehicle_has_valid_grz(self):
        assert "А123ВС777" in self.row.vehicle

    def test_reception(self):
        assert "15.03.2024" in self.row.reception
        assert "Складская" in self.row.reception

    def test_confidence_is_high(self):
        assert self.row.confidence.overall() >= 0.7
        assert self.row.confidence.vehicle == 1.0  # валидированный ГРЗ


class TestMessyWaybill:
    def setup_method(self) -> None:
        rows = parse_text(_text("tn_messy.txt"), "tn_messy.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_date(self):
        assert self.row.date == "07.11.2023"

    def test_number(self):
        assert self.row.number == "А-99/2023"

    def test_shipper_confusables_ok(self):
        # Нормализация конвертировала «ИHH» → «ИНН».
        assert "Сидоров" in self.row.shipper

    def test_carrier(self):
        assert "ТрансЛайн" in self.row.carrier

    def test_vehicle_grz(self):
        assert "К456МН178" in self.row.vehicle


class TestMultiWaybillsInOneFile:
    def test_splits_into_two(self):
        rows = parse_text(_text("tn_multi.txt"), "tn_multi.pdf")
        assert len(rows) == 2

        assert rows[0].number == "001"
        assert rows[0].date == "01.01.2024"
        assert "Альфа" in rows[0].shipper
        assert "А001АА77" in rows[0].vehicle
        assert rows[0].source.endswith("#1")

        assert rows[1].number == "002"
        assert rows[1].date == "02.01.2024"
        assert "Гамма" in rows[1].shipper
        assert "В002ВВ77" in rows[1].vehicle
        assert rows[1].source.endswith("#2")


class TestEmptyInput:
    def test_returns_placeholder_row(self):
        rows = parse_text("", "empty.pdf")
        assert len(rows) == 1
        r = rows[0]
        assert r.date == MISSING
        assert r.number == MISSING
        assert "LOW_TEXT" in r.note
        assert r.confidence.overall() == 0.0
