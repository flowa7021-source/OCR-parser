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

    def test_volume(self):
        assert "10 000" in self.row.volume or "10000" in self.row.volume.replace(" ", "")
        assert "кг" in self.row.volume.lower() or "брутто" in self.row.volume.lower()

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

    def test_to_excel_tuple_has_12_columns(self):
        # 12 колонок: waybill, date, number, shipper, consignee,
        # cargo, volume, carrier, vehicle, reception, source, note.
        assert len(self.row.to_excel_tuple()) == 12


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

    def test_carrier_is_driver_when_samovyvoz_standalone(self):
        # При самовывозе «перевозчик» по сути — водитель. Если «Самовывоз»
        # стоит на отдельной строке, а ФИО водителя — на следующей, в
        # карточке перевозчика оставляем только ФИО (юрлица-перевозчика нет).
        assert "Рябов" in self.row.carrier
        assert "Самовывоз" not in self.row.carrier
        # Служебные «(реквизиты, позволяющие…)» не попадают.
        assert "(реквизиты" not in self.row.carrier

    def test_vehicle_is_grz_only(self):
        # Марку из поля «Транспортное средство» не извлекаем (OCR бланков
        # систематически «перекрывает» латиницу кириллицей, делая марку
        # неразборчивой). Храним только канонический ГРЗ.
        assert "Р 814 НР 152" in self.row.vehicle
        assert "RENAULT" not in self.row.vehicle

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

    def test_vehicle_has_no_brand(self):
        # Марка не извлекается — её OCR ненадёжен. В поле остаётся только ГРЗ.
        assert "RENAULT" not in self.row.vehicle

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

    def test_cargo_strips_ocr_naim_prefix(self):
        # «1. Нанменование — Блок облицовочный» — OCR-вариант «Наименование».
        # Префикс должен быть выпилен, значение начинается с «Блок».
        assert "Блок облицовочный" in self.row.cargo
        assert "Нанменование" not in self.row.cargo
        assert "наименование" not in self.row.cargo.lower()

    def test_carrier_no_short_noise(self):
        # «/ Й /» — OCR-мусор из границы ячейки таблицы, не должен попасть.
        assert "/ Й /" not in self.row.carrier
        assert "Самовывоз" in self.row.carrier


class TestOcrInlineLayout:
    """Заголовок ТН: всё в одной строке.

    Реальный OCR двухколоночных форм иногда помещает заголовок, «Экземпляр №»,
    «Дата» и «№ номер» на ОДНУ строку (левая и правая колонки читаются слева
    направо подряд). Это порождает два системных бага:

    1. Номер: «Экземпляр №» стоит ЛЕВЕЕ реального «№ 7145/Б» на той же строке
       → старая логика включала «экземпляр» в same-line контекст для ОБОИХ «№»
       и пропускала правильный номер.

    2. ТС: когда марка и ГРЗ на одной строке («RENAULT Р 814 НР 152»), строка
       полностью пропускалась и марка не извлекалась.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_ocr_inline.txt"), "tn_ocr_inline.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_number_extracted_despite_ekzemplyar_same_line(self):
        # «Экземпляр №» стоит левее настоящего «№ 7145/Б» на той же строке.
        assert self.row.number == "7145/Б"

    def test_date_extracted(self):
        assert self.row.date == "23.07.2022"

    def test_vehicle_brand_dropped_inline_layout(self):
        # «RENAULT Р 814 НР 152» — марка в поле не попадает, остаётся только ГРЗ.
        assert "RENAULT" not in self.row.vehicle

    def test_vehicle_grz_extracted_from_inline_line(self):
        assert "Р 814 НР 152" in self.row.vehicle

    def test_shipper_ok(self):
        assert "Бекам" in self.row.shipper
        assert "7743553262" in self.row.shipper

    def test_consignee_ok(self):
        assert "Моспроект" in self.row.consignee

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


class TestRealOcrFormat:
    """Реальный формат OCR: табличная разбивка строк из двухколоночного PDF.

    Паттерны, которых нет в других фикстурах:
    1. Номер в ячейке «№ — |7145/Б» (с pipe-сепаратором); заголовок ТН при
       этом OCR-читается как «№7145/6» (6 вместо Б).
    2. Аннотация грузоотправителя разбита: «Заказчик услуг по организации» на
       одной строке, «перевозки груза (при наличии)» — на следующей.
    3. Наименование груза на отдельной строке: «1. Нанменование —» без значения,
       «Блок облицовочный...» — на следующей.
    4. Объём разбит через тире: «Нетто —\n20,52 т., Брутто —\n20,835 т.,...»
    5. Перевозчик: «— (реквизиты, позволяющие идентифицировать водителя(-ей))»
       должен фильтроваться.
    6. Приём груза: аннотации «(адрес места погрузки)» и подобные — фильтруются.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_real_ocr.txt"), "tn_real_ocr.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_number_from_pipe_table_cell(self):
        # «№ — |7145/Б» — pipe-сепаратор не должен мешать.
        # Заголовок «НАКЛАДНАЯ №7145/6» (OCR-ошибка) не должен перебить.
        assert self.row.number == "7145/Б"

    def test_date(self):
        assert self.row.date == "23.07.2022"

    def test_shipper_no_multiline_service_annotation(self):
        # «перевозки груза (при наличии)» на отдельной строке — не значение.
        assert "перевозки груза" not in self.row.shipper.lower()
        assert "Бекам" in self.row.shipper
        assert "7743553262" in self.row.shipper

    def test_cargo_strips_standalone_prefix_line(self):
        # «1. Нанменование —» (строка без значения) должна быть отброшена.
        assert "Блок облицовочный" in self.row.cargo
        assert "Нанменование" not in self.row.cargo
        # Весовые/объёмные данные не должны попадать в наименование груза.
        assert "Брутто" not in self.row.cargo
        assert "Объем" not in self.row.cargo
        assert "20,52" not in self.row.cargo

    def test_volume_joins_split_lines(self):
        # «Нетто —\n20,52 т., Брутто —\n20,835 т., Объем —\n8,73 м³»
        # должно склеиться в одну строку.
        v = self.row.volume
        assert "20,52" in v
        assert "20,835" in v
        assert "8,73" in v

    def test_carrier_no_driver_annotation(self):
        # «— (реквизиты, позволяющие идентифицировать водителя(-ей))» — мусор.
        assert "реквизиты" not in self.row.carrier.lower()
        assert "идентифицировать" not in self.row.carrier.lower()
        assert "Рябов" in self.row.carrier

    def test_reception_no_form_annotations(self):
        # «(адрес места погрузки)» и подобные — форм-аннотации, не значение.
        r = self.row.reception
        assert "(адрес места погрузки)" not in r
        assert "(заявленные дата" not in r
        assert "Подолино" in r
        assert "23.07.2022" in r


class TestRealOcrV2:
    """Полный «сырой» дамп PyMuPDF реальной ТН №7145/Б с двухколоночной формой.

    Сценарии, сложившиеся в одной фикстуре:
    - Шаблонные хвосты в грузоотправителе («отличным от грузоотправителя…»).
    - «Доугая нсобходимая информация о грузе)» и {-шаблоны в груз.
    - «7. Транспортное средств6 ‚^» — OCR-типос в заголовке ТС.
    - «_ ВРМАЗЕТ / Р 814 НР 152» — марка + ГРЗ через OCR-мусор.
    - Самовывоз + водитель на разных строках → в перевозчике только водитель.
    - «Нетто —\\n20,52 т., Брутто —\\n…» — объём, склеиваемый через тире.
    - В «Приём груза» два прочтения ООО «Бекам» и хвост шаблона формы.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_real_ocr_v2.txt"), "tn_real_ocr_v2.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_number_from_table_cell(self):
        # В сыром тексте заголовок прочитан как «№7145/6», но табличная
        # ячейка «№ — |7145/Б» даёт правильный номер с кириллической «Б».
        assert self.row.number == "7145/Б"

    def test_date(self):
        assert self.row.date == "23.07.2022"

    def test_shipper_no_service_tail(self):
        # «отличным от грузоотправителя (при наличии)» — шаблонная метка,
        # не значение; не должна попасть в реквизиты грузоотправителя.
        s = self.row.shipper
        assert "Бекам" in s
        assert "7743553262" in s
        assert "отличным" not in s.lower()
        assert "(при наличи" not in s.lower()
        # «ло организалии пёеревозки груза)» — OCR-вариант «по организации
        # перевозки груза)»; это хвост шаблонной аннотации, в значение не попадает.
        assert "организ" not in s.lower()
        assert "перевозк" not in s.lower()
        # Висячий ярлык «КПП» без значения — срезается.
        assert not s.rstrip().endswith("КПП")
        assert "ИНН 7743553262, КПП," not in s

    def test_cargo_clean(self):
        # «Блок облицовочный…720 нтт» — без шаблонных приложений формы.
        c = self.row.cargo
        assert c.startswith("Блок облицовочный")
        assert "Наименование" not in c
        assert "необходим" not in c.lower()
        assert "масса груза" not in c.lower()
        assert "ДОПОГ" not in c
        assert "при необходимости" not in c.lower()
        # И никакого «{» / «[» шаблонного пролога.
        assert "{" not in c
        assert "[" not in c

    def test_volume_has_all_three_values(self):
        v = self.row.volume
        assert "20,52" in v
        assert "20,835" in v
        assert "8,73" in v
        # Висячего тире в конце быть не должно.
        assert not v.rstrip().endswith("—")
        assert not v.rstrip().endswith("-")

    def test_carrier_only_driver(self):
        # Самовывоз + отдельная строка с ФИО → только ФИО.
        c = self.row.carrier
        assert c == "Рябов В.К." or c.startswith("Рябов В.К.")
        assert "Самовывоз" not in c
        assert "реквизиты" not in c.lower()

    def test_vehicle_only_grz_no_brand(self):
        v = self.row.vehicle
        # Держим в поле только ГРЗ. «ВРМАЗЕТ» — это OCR-перекладка латинского
        # «RENAULT» кириллицей; в итоговую строку не попадает.
        assert v == "Р 814 НР 152"
        assert "ВРМАЗЕТ" not in v
        assert "RENAULT" not in v

    def test_reception_is_clean(self):
        r = self.row.reception
        # Есть основная шапка, адрес погрузки и дата.
        assert "Бекам" in r
        assert "Подолино" in r
        assert "23.07.2022" in r
        # Повторы блока «ООО Бекам» убраны — «Бекам» встречается один раз.
        assert r.lower().count("бекам") == 1
        # Хвост шаблона формы не попал в значение.
        assert "масса груза брутто" not in r.lower()
        assert "взвеш" not in r.lower()
        assert "расчетная масс" not in r.lower()
        # Нет висячего «Нетто —» / «Брутто —» (данные выводятся в volume).
        assert "Нетто" not in r
        assert "Брутто" not in r
        # Нет следующего раздела «Переадресовка/Выдача».
        assert "Переадресовка" not in r
        assert "Выдача" not in r

    def test_reception_blocks_separated_by_blank_line(self):
        r = self.row.reception
        # Блоки «реквизиты | адрес | дата» разделены пустой строкой.
        blocks = [b.strip() for b in r.split("\n\n") if b.strip()]
        assert len(blocks) >= 3
        # Компания — в первом блоке.
        assert "Бекам" in blocks[0]
        # Адрес погрузки — в отдельном блоке, склеен в одну строку
        # (индекс + область/с-п/р-н + населённый пункт), без «куча в кучу».
        address_block = next(b for b in blocks if "Подолино" in b)
        assert "141411" in address_block
        assert "Московская" in address_block
        assert "\n" not in address_block  # одна строка, а не каша
        # Дата — отдельным блоком.
        assert any(b.strip() == "23.07.2022" for b in blocks)

    def test_reception_has_innn_not_trimmed(self):
        # «Ин 7743553262» → «ИНН 7743553262» (восстанавливаем 3-ю букву,
        # часто теряемую OCR на границе ячейки).
        assert "ИНН 7743553262" in self.row.reception


# ---------------------------------------------------------------------------


_CARGO_QTY_ONLY = """\
1. Грузоотправитель
ООО Тест, ИНН 1234567890

2. Грузополучатель
ИП Иванов, г. Воронеж

3. Груз
Кирпич строительный М-150, красный, 500 шт

6. Перевозчик
ООО Транс

7. Транспортное средство
КАМАЗ А 123 БВ 36
"""


class TestVolumeQuantityFallback:
    """Когда нет строк Нетто/Брутто — объём берётся из количества на строке груза."""

    def setup_method(self) -> None:
        from tn_parser.normalize import normalize_for_sections
        rows = parse_text(normalize_for_sections(_CARGO_QTY_ONLY), "test.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_volume_from_quantity(self):
        # Нет строк «Нетто/Брутто» — должны взять «500 шт» как объём.
        v = self.row.volume
        assert "500" in v
        assert "шт" in v.lower()

    def test_cargo_still_has_name(self):
        assert "Кирпич" in self.row.cargo


class TestCargoTemplateTextFiltered:
    """Шаблонные фразы из бланка ТН не попадают в поле «Груз»."""

    _TEXT = """\
1. Грузоотправитель
ООО Отправитель, ИНН 9876543210

2. Грузополучатель
ООО Получатель, г. Самара

3. Груз
1. Наименование —
Щебень гранитный фр. 5-20, 30 т
(отгрузочное наименование груза (для опасных грузов — в соответствии с ДОПОГ/МГ),
его состояние и)
Нетто — 30 т., Брутто — 30,5 т.

6. Перевозчик
ИП Петров

7. Транспортное средство
VOLVO В 456 МН 63
"""

    def setup_method(self) -> None:
        from tn_parser.normalize import normalize_for_sections
        rows = parse_text(normalize_for_sections(self._TEXT), "test.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_cargo_no_template_text(self):
        assert "отгрузочное" not in self.row.cargo.lower()
        assert "опасных грузов" not in self.row.cargo.lower()
        assert "его состояние" not in self.row.cargo.lower()

    def test_cargo_has_name(self):
        assert "Щебень" in self.row.cargo

    def test_volume_has_weight(self):
        assert "30" in self.row.volume
