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
        # Канонический вид: "А 123 ВС 777".
        assert "А 123 ВС 777" in self.row.vehicle
        assert "А123ВС777" in self.row.vehicle.replace(" ", "")

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
        # Канонический вид ГРЗ: "Р 814 НР 152".
        assert "Р 814 НР 152" in self.row.vehicle

    def test_cargo_keeps_latin_in_mixed_word(self):
        # «Тенsar» — латинская «a» должна остаться латинской.
        assert "sar" in self.row.cargo  # латинский кластер сохранён
        assert "\u0441\u0430r" not in self.row.cargo  # не кириллическая «с а»

    def test_reception_stops_at_next_section(self):
        r = self.row.reception
        assert "Подолино" in r
        assert "23.07.2022" in r
        # Следующие разделы не утекли.
        assert "Переадресовка" not in r
        assert "Выдача груза" not in r


class TestOcrTabularLayout:
    """Интеграция на OCR-слое реальной табличной формы ТН №7145/Б.

    Имитирует выдачу PyMuPDF для двухколоночной/табличной формы: ячейки
    заголовков и значений стоят на разных строках, в разделах 6/7 — два
    значения (способ | ФИО, марка | ГРЗ).
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_ocr_tabular.txt"), "tn_ocr_tabular.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_number_not_missed_due_to_ekzemplyar(self):
        # Рядом стоит «Экземпляр №» — ложный маркер. Правильный номер: 7145/Б.
        assert self.row.number == "7145/Б"
        assert self.row.waybill == "Транспортная накладная № 7145/Б"

    def test_date(self):
        assert self.row.date == "23.07.2022"

    def test_shipper_skips_service_line(self):
        # «является экспедитором» — служебная подпись, не значение.
        assert "является экспедитором" not in self.row.shipper.lower()
        # И не захватывает пояснение в скобках.
        assert "(реквизиты" not in self.row.shipper
        assert "Бекам" in self.row.shipper
        # ИНН/КПП остаются в реквизитах.
        assert "7743553262" in self.row.shipper
        assert "504445001" in self.row.shipper

    def test_consignee(self):
        assert "Моспроект" in self.row.consignee
        assert "7707820890" in self.row.consignee
        assert "(реквизиты" not in self.row.consignee

    def test_cargo_strips_prefix(self):
        assert self.row.cargo.startswith("Блок облицовочный")
        assert "Наименование" not in self.row.cargo
        assert "1." not in self.row.cargo[:5]

    def test_carrier_combines_two_columns(self):
        # Две колонки: «Самовывоз» + «Рябов В.К.».
        assert "Самовывоз" in self.row.carrier
        assert "Рябов" in self.row.carrier
        # Служебные «(реквизиты, позволяющие…)» не попадают.
        assert "(реквизиты" not in self.row.carrier

    def test_vehicle_combines_marka_and_grz(self):
        # Формат: «RENAULT Р 814 НР 152».
        assert "RENAULT" in self.row.vehicle
        assert "Р 814 НР 152" in self.row.vehicle

    def test_reception_no_next_section_leak(self):
        r = self.row.reception
        assert "Подолино" in r
        assert "Переадресовка" not in r
        assert "Выдача груза" not in r

    def test_confidence_high(self):
        assert self.row.confidence.overall() >= 0.85


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


class TestOcrNoise:
    """Реальный OCR-шум: три системные проблемы.

    1. Номер: «Экземпляр №» стоит на строке ВЫШЕ реального «№ 7145/Б» →
       left-context-окно в 20 символов ошибочно включало «экземпляр» из
       предыдущей строки и пропускало правильный номер.

    2. ГРЗ: ИНН 7743553262 в разделе «Транспортное средство» после
       компактизации пробелов даёт «ИНН7743553262», откуда «НН7743553»
       ложно распознаётся как прицепной ГРЗ.

    3. Грузоотправитель/Приём: фраза «Заказчик услуг по организации
       перевозки груза» и короткие OCR-мусорные строки («Г», «а.», «ГЕР»)
       прилипают к содержательному блоку.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_ocr_noise.txt"), "tn_ocr_noise.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    # --- Проблема 1: номер -----------------------------------------------

    def test_number_extracted_despite_ekzemplyar_on_prev_line(self):
        # «Экземпляр №» на строке выше «№ 7145/Б» не должен блокировать
        # извлечение реального номера.
        assert self.row.number == "7145/Б"

    def test_date_extracted(self):
        assert self.row.date == "23.07.2022"

    # --- Проблема 2: ГРЗ vs ИНН ------------------------------------------

    def test_vehicle_grz_not_inn_false_positive(self):
        # ИНН 7743553262 в секции ТС не должен стать «НН 7743 553».
        assert "НН 7743 553" not in self.row.vehicle
        assert "7743553262" not in self.row.vehicle

    def test_vehicle_has_real_grz(self):
        assert "Р 814 НР 152" in self.row.vehicle

    def test_vehicle_has_brand(self):
        assert "RENAULT" in self.row.vehicle

    # --- Проблема 3: шум в грузоотправителе/приёме -----------------------

    def test_shipper_no_zakazchik_prefix(self):
        # «Заказчик услуг по организации перевозки груза» — служебная метка.
        assert "заказчик" not in self.row.shipper.lower()

    def test_shipper_no_short_noise(self):
        # «Га» (OCR-мусор) не должен попасть в начало поля.
        assert not self.row.shipper.startswith("Га")
        assert "Бекам" in self.row.shipper
        assert "7743553262" in self.row.shipper

    def test_reception_no_ocr_garbage(self):
        # Строки «'|_' г-.:», «Г», «а.», «ГЕР» — чистый OCR-мусор.
        r = self.row.reception
        assert "ГЕР" not in r
        assert "Бекам" in r

    def test_reception_no_ukrainian_letters(self):
        # Украинские і/ї/є/ґ в русских ТН не встречаются — всегда OCR-шум.
        r = self.row.reception
        for ch in "іїєґ":
            assert ch not in r, f"ukrainian letter {ch!r} leaked into reception"

    def test_reception_no_embedded_quotes(self):
        # «Бекам'тбд», «'Г'чп» — апострофы внутри слов должны быть выпилены.
        r = self.row.reception
        assert "Бекам'" not in r
        assert "'Г'" not in r
        assert "'|_'" not in r

    def test_shipper_no_ukrainian_letters(self):
        s = self.row.shipper
        for ch in "іїєґ":
            assert ch not in s, f"ukrainian letter {ch!r} leaked into shipper"

    def test_reception_has_no_residual_junk(self):
        # «000 д», «11 [3] Т» после чистки не должны оставаться.
        r = self.row.reception
        for junk in ("000 д", "11 [3]", " д\n", "\nд\n"):
            assert junk not in r, f"residual junk {junk!r} in reception"

    def test_shipper_has_no_service_tail(self):
        # «перевозки груза (при наличии)» — служебный хвост.
        s = self.row.shipper.lower()
        assert "(при наличи" not in s
        assert "перевозки груза" not in s
