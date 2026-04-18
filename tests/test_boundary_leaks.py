# -*- coding: utf-8 -*-
"""Тесты на системную проблему: парсер захватывает лишний текст до/после
искомого значения либо «тянет» в поле содержимое соседней графы.

Это TDD-красная фаза для задачи «end-anchors в fields.py». Фикстуры имитируют
три архетипа жалоб пользователя:

    1. suffix-leak    — после значения тянутся подписи/печати/комментарии
    2. prefix-leak    — перед значением остаются служебные строки
    3. graph confusion — OCR потерял точку в номере графы («6 Перевозчик»
                         вместо «6. Перевозчик»), и предыдущая секция
                         поглощает следующую
    4. inline-leak    — fallback-regex захватывает соседнюю колонку на
                         той же строке
"""

from pathlib import Path

from tn_parser import parse_text
from tn_parser.normalize import normalize_for_sections


FIXTURES = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return normalize_for_sections((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Архетип 1: suffix-leak — подпись/печать/контакт после реквизитов
# ---------------------------------------------------------------------------


class TestSuffixLeak:
    """В разделе контрагента стоят реквизиты, а ниже — «Подпись: Петров»,
    «МП», «Контактное лицо: …». Парсер обязан оставить только реквизиты.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_suffix_leak.txt"), "tn_suffix_leak.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_shipper_stops_before_signature(self):
        # «Подпись грузоотправителя: Петров П.П.» и «МП» не должны утечь.
        s = self.row.shipper
        assert "Альфа-Металл" in s
        assert "7801234567" in s  # ИНН остаётся
        assert "Подпись" not in s
        assert "Петров" not in s
        assert "МП" not in s.split()  # отдельным токеном тоже быть не должно

    def test_consignee_stops_before_contact_person(self):
        c = self.row.consignee
        assert "Бета-Пром" in c
        assert "7701098765" in c
        # «Контактное лицо» — это НЕ часть реквизитов юрлица, а служебная строка.
        assert "Контактное лицо" not in c
        assert "Сидорова" not in c

    def test_cargo_stops_at_technical_attrs(self):
        # «Наименование — Прокат …» — основное значение. Классы, упаковка
        # и массы идут ниже и НЕ должны попадать в поле «Груз».
        cg = self.row.cargo
        assert "Прокат стальной" in cg
        assert "Класс опасности" not in cg
        assert "Упаковка" not in cg

    def test_carrier_stops_at_section_end(self):
        car = self.row.carrier
        assert "ТрансЛогистик" in car
        assert "7805667788" in car
        # VOLVO — это ТС из следующего раздела, не должен утечь.
        assert "VOLVO" not in car
        assert "О 777" not in car


# ---------------------------------------------------------------------------
# Архетип 2: graph confusion — OCR потерял точку в номере графы
# ---------------------------------------------------------------------------


class TestGraphConfusion:
    """OCR прочитал «6 Перевозчик» без точки после цифры. Существующая
    регулярка `\\d{1,2}\\s*[.)]\\s*` в `_NUMBERED` этого не ловит, и
    секция «Перевозчик» сливается с предыдущей. Парсер должен устоять
    либо за счёт `_BARE`-заголовков, либо за счёт более мягкой регулярки.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_graph_confusion.txt"), "tn_graph_confusion.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_carrier_extracted_despite_missing_dot(self):
        # «6 Перевозчик» без точки. Значение — ИП Захаров.
        car = self.row.carrier
        assert "Захаров" in car
        assert "165700998877" in car

    def test_cargo_does_not_leak_into_carrier(self):
        # В секции карго — картон, но часто при graph confusion ИП Захаров
        # и номер графы 6 склеиваются в одно. Не должно быть наоборот —
        # карго не должно затягивать перевозчика.
        cg = self.row.cargo
        assert "Картон" in cg
        assert "Захаров" not in cg
        assert "Перевозчик" not in cg

    def test_reception_extracted_despite_missing_dot(self):
        # «8 Приём груза» без точки.
        r = self.row.reception
        assert "Баумана" in r
        assert "02.02.2024" in r
        # Следующий раздел не утёк.
        assert "Переадресовка" not in r

    def test_vehicle_extracted(self):
        # «7. Транспортное средство» — номер в порядке, проверка sanity.
        # Также не должно утекать содержимое раздела 8 (Приём груза).
        v = self.row.vehicle
        assert "VOLVO" in v
        assert "В 404 КМ 716" in v
        assert "Приём" not in v
        assert "Баумана" not in v


# ---------------------------------------------------------------------------
# Архетип 3: inline-leak — две графы на одной строке, fallback-regex тянет
# соседнюю колонку
# ---------------------------------------------------------------------------


class TestInlineLeak:
    """OCR двухколоночной формы иногда склеивает «Грузоотправитель: X» и
    «Грузополучатель: Y» в ОДНУ строку. Секция-парсер не видит заголовка
    получателя (он не начинает строку), и fallback-regex extract_org берёт
    всё подряд до конца строки. Нужен end-anchor на начало следующего поля.
    """

    def setup_method(self) -> None:
        rows = parse_text(_text("tn_inline_leak.txt"), "tn_inline_leak.pdf")
        assert len(rows) == 1
        self.row = rows[0]

    def test_shipper_does_not_absorb_consignee_on_same_line(self):
        s = self.row.shipper
        assert "Омега" in s
        assert "7712345678" in s
        # Грузополучатель не должен утечь в грузоотправителя.
        assert "Сигма" not in s
        assert "7798765432" not in s
        assert "Грузополучатель" not in s

    def test_consignee_extracted_separately(self):
        c = self.row.consignee
        # «Сигма» должна быть извлечена как получатель.
        assert "Сигма" in c
        assert "7798765432" in c
        # Омега (отправитель) не должна попасть к получателю.
        assert "Омега" not in c
