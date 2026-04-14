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

    def test_consignee(self):
        assert "Василёк" in self.row.consignee

    def test_cargo(self):
        assert "Мука" in self.row.cargo

    def test_carrier(self):
        assert "Быстрые Перевозки" in self.row.carrier

    def test_vehicle_has_valid_grz(self):
        assert "А123ВС777" in self.row.vehicle

    def test_reception(self):
        assert "15.03.2024" in self.row.reception
        assert "Складская" in self.row.reception

    def test_confidence_is_high(self):
        assert self.row.confidence.overall() >= 0.7
        assert self.row.confidence.vehicle == 1.0

    def test_to_excel_tuple_has_11_columns(self):
        # 11 колонок: waybill, date, number, shipper, consignee,
        # cargo, carrier, vehicle, reception, source, note.
        assert len(self.row.to_excel_tuple()) == 11


class TestRealSample7145B:
    """Интеграция на реальном образце (ТН №7145/Б от 23.07.2022)."""

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_real_7145B.txt"), "tn_7145B.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_number_and_date(self):
        assert self.row.number == "7145/Б"
        assert self.row.date == "23.07.2022"

    def test_shipper_not_service_stub(self):
        # «является экспедитором» не должно попасть как значение.
        assert "является экспедитором" not in self.row.shipper.lower()
        assert "Бекам" in self.row.shipper
        assert "7743553262" in self.row.shipper  # ИНН остаётся в реквизитах

    def test_consignee_extracted(self):
        assert "Моспроект" in self.row.consignee
        assert "7707820890" in self.row.consignee

    def test_cargo_handles_dash_separator(self):
        # "1. Наименование — Блок облицовочный ..." → "Блок облицовочный ..."
        assert "Блок облицовочный" in self.row.cargo
        assert "Наименование" not in self.row.cargo

    def test_carrier_samovyvoz(self):
        assert "Самовывоз" in self.row.carrier

    def test_vehicle_has_grz_with_spaces(self):
        # ГРЗ в документе: "Р 814 НР 152" (пробелы между частями).
        v = self.row.vehicle.replace(" ", "")
        assert "Р814НР152" in v

    def test_reception_stops_at_next_section(self):
        r = self.row.reception
        assert "Подолино" in r
        assert "23.07.2022" in r
        # Следующие разделы не утекли.
        assert "Переадресовка" not in r
        assert "Выдача груза" not in r


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
        assert "Сидоров" in self.row.shipper

    def test_carrier(self):
        assert "ТрансЛайн" in self.row.carrier

    def test_vehicle_grz(self):
        assert "К456МН178" in self.row.vehicle.replace(" ", "")


class TestMultiWaybillsInOneFile:
    def test_splits_into_two(self):
        rows = parse_text(_text("tn_multi.txt"), "tn_multi.pdf")
        assert len(rows) == 2

        assert rows[0].number == "001"
        assert rows[0].date == "01.01.2024"
        assert "Альфа" in rows[0].shipper
        assert "А001АА77" in rows[0].vehicle.replace(" ", "")

        assert rows[1].number == "002"
        assert rows[1].date == "02.01.2024"
        assert "Гамма" in rows[1].shipper
        assert "В002ВВ77" in rows[1].vehicle.replace(" ", "")


class TestEmptyInput:
    def test_returns_placeholder_row(self):
        rows = parse_text("", "empty.pdf")
        assert len(rows) == 1
        r = rows[0]
        assert r.date == MISSING
        assert r.number == MISSING
        assert r.consignee == MISSING
        assert "LOW_TEXT" in r.note
        assert r.confidence.overall() == 0.0
