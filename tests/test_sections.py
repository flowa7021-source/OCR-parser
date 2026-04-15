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
    assert "Блок облицовочный" in sec["cargo"]
    assert "Самовывоз" in sec["carrier"]
    assert "RENAULT" in sec["vehicle"]
    assert "Р 814" in sec["vehicle"]
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


# ---------------------------------------------------------------------------
# Синонимы и OCR-варианты: проверка, что альтернативные названия заголовков
# резолвятся в правильные роли. Эти тесты — страховка от случайного удаления
# синонима из _ROLE_TITLES при чистке кода.
# ---------------------------------------------------------------------------


def _mini(shipper_kw: str, consignee_kw: str,
          cargo_kw: str, carrier_kw: str,
          vehicle_kw: str, reception_kw: str) -> str:
    """Собирает мини-ТН с произвольными названиями разделов."""
    return (
        f"Транспортная накладная № TST-1 от 01.01.2024\n"
        f"\n1. {shipper_kw}\n"
        f"ООО «Альфа», ИНН 7701111111\n"
        f"г. Москва, ул. Пушкина, д. 1\n"
        f"\n2. {consignee_kw}\n"
        f"ООО «Бета», ИНН 7702222222\n"
        f"г. Подольск, ул. Чехова, д. 5\n"
        f"\n3. {cargo_kw}\n"
        f"Комплектующие металлические, 100 шт\n"
        f"\n6. {carrier_kw}\n"
        f"ООО «Гамма»\n"
        f"\n7. {vehicle_kw}\n"
        f"А123ВС77\n"
        f"\n8. {reception_kw}\n"
        f"г. Москва, склад 10, 01.01.2024\n"
    )


class TestRoleSynonyms:
    """Для каждого альтернативного заголовка проверяем, что роль определяется."""

    def test_shipper_synonyms(self):
        for kw in (
            "Грузоотправитель",         # канон
            "Отправитель груза",        # синоним
            "Продавец-грузоотправитель",
            "Поставщик груза",
        ):
            sec = split_sections(normalize_for_sections(
                _mini(kw, "Грузополучатель", "Груз",
                      "Перевозчик", "Транспортное средство", "Прием груза")
            ))
            assert "shipper" in sec, f"kw={kw!r} не распознан как shipper"
            assert "Альфа" in sec["shipper"]

    def test_consignee_synonyms(self):
        for kw in (
            "Грузополучатель",
            "Получатель груза",
            "Покупатель-грузополучатель",
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", kw, "Груз",
                      "Перевозчик", "Транспортное средство", "Прием груза")
            ))
            assert "consignee" in sec
            assert "Бета" in sec["consignee"]

    def test_cargo_synonyms(self):
        for kw in (
            "Груз",
            "Сведения о грузе",          # [1-Т]
            "Товарный раздел",           # [1-Т]
            "Наименование груза",        # [синоним]
            "Предмет перевозки",         # [синоним]
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", "Грузополучатель", kw,
                      "Перевозчик", "Транспортное средство", "Прием груза")
            ))
            assert "cargo" in sec, f"kw={kw!r} не распознан как cargo"
            assert "Комплектующие" in sec["cargo"]

    def test_carrier_synonyms(self):
        for kw in (
            "Перевозчик",
            "Организация-владелец автотранспорта",   # [1-Т]
            "Владелец автотранспорта",               # [1-Т]
            "Автоперевозчик",                        # [синоним]
            "Транспортная организация",              # [синоним]
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", "Грузополучатель", "Груз",
                      kw, "Транспортное средство", "Прием груза")
            ))
            assert "carrier" in sec, f"kw={kw!r} не распознан как carrier"
            assert "Гамма" in sec["carrier"]

    def test_vehicle_synonyms(self):
        for kw in (
            "Транспортное средство",
            "Марка автомобиля",         # [1-Т]
            "Государственный номерной знак",   # [1-Т]
            "Сведения о транспортном средстве",
            "Автотранспорт",
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", "Грузополучатель", "Груз",
                      "Перевозчик", kw, "Прием груза")
            ))
            assert "vehicle" in sec, f"kw={kw!r} не распознан как vehicle"
            assert "А123ВС77" in sec["vehicle"]

    def test_reception_synonyms(self):
        for kw in (
            "Прием груза",
            "Приём груза",
            "Погрузка груза",
            "Пункт погрузки",                   # [1-Т]
            "Место погрузки",                   # [синоним]
            "Информация о принятии груза к перевозке",
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", "Грузополучатель", "Груз",
                      "Перевозчик", "Транспортное средство", kw)
            ))
            assert "reception" in sec, f"kw={kw!r} не распознан как reception"
            assert "склад" in sec["reception"].lower()


class TestOcrGarbledHeaders:
    """Ключевые слова с OCR-искажениями (Левенштейн 1–2)."""

    def test_shipper_ocr_variants(self):
        for kw in (
            "Грузоатиравитель",     # 2 substitutions
            "Грузоотиравитель",     # 1 substitution
            "Срузосотправитель",    # 1 insert + 1 substitution
            "Грузоотпровитель",     # 1 substitution (а→о)
            "Грузоотпранитель",     # 1 substitution
        ):
            sec = split_sections(normalize_for_sections(
                _mini(kw, "Грузополучатель", "Груз",
                      "Перевозчик", "Транспортное средство", "Прием груза")
            ))
            assert "shipper" in sec, f"OCR-вариант {kw!r} не распознан"

    def test_consignee_ocr_variants(self):
        for kw in (
            "Грузопоручатель",     # 1 sub
            "Грузололучатель",     # 1 sub
            "Грузополучат ель",    # 1 insert (пробел)
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", kw, "Груз",
                      "Перевозчик", "Транспортное средство", "Прием груза")
            ))
            assert "consignee" in sec, f"OCR-вариант {kw!r} не распознан"

    def test_carrier_ocr_variants(self):
        for kw in (
            "Лерезозчик",      # 2 sub
            "Первозчик",       # 1 deletion
            "Пэревозчик",      # 1 sub
        ):
            sec = split_sections(normalize_for_sections(
                _mini("Грузоотправитель", "Грузополучатель", "Груз",
                      kw, "Транспортное средство", "Прием груза")
            ))
            assert "carrier" in sec, f"OCR-вариант {kw!r} не распознан"

    def test_heavy_ocr_whitelist(self):
        """Тяжёлые OCR-варианты (3+ правок), покрытые явным whitelist."""
        cases = [
            # (OCR-шапка, роль, ожидаемая подстрока в найденной секции)
            ("Трузоотправитель",     "shipper",    "Альфа"),
            ("Груэоотправитель",     "shipper",    "Альфа"),
            ("Грузоотправитепь",     "shipper",    "Альфа"),   # Левенштейн-1
            ("Трузополучатель",      "consignee",  "Бета"),
            ("Лерезозчик",           "carrier",    "Гамма"),
            ("Перееозчик",           "carrier",    "Гамма"),
            ("Приъм груза",          "reception",  "склад"),
        ]
        for kw, role, needle in cases:
            # Собираем мини-ТН, в которой ИМЕННО этот заголовок искажён,
            # а остальные — каноничные.
            kws = {
                "shipper":   "Грузоотправитель",
                "consignee": "Грузополучатель",
                "cargo":     "Груз",
                "carrier":   "Перевозчик",
                "vehicle":   "Транспортное средство",
                "reception": "Прием груза",
            }
            kws[role] = kw
            sec = split_sections(normalize_for_sections(_mini(
                kws["shipper"], kws["consignee"], kws["cargo"],
                kws["carrier"], kws["vehicle"], kws["reception"],
            )))
            assert role in sec, f"OCR-шапка {kw!r} не распознана как {role}"
            assert needle.lower() in sec[role].lower(), (
                f"{kw!r} распознан как {role}, но контент некорректен: "
                f"{sec.get(role)!r}"
            )

    def test_junky_numeric_prefix(self):
        """«&, Перевозчик», «5;», «|_| 1.» — OCR испортил номер-префикс."""
        for prefix in ("&, ", "5; ", "|_ 1. ", "% "):
            text = normalize_for_sections(
                f"Транспортная накладная № TST-1 от 01.01.2024\n"
                f"1. Грузоотправитель\n"
                f"ООО «Альфа», ИНН 7701111111\n"
                f"2. Грузополучатель\n"
                f"ООО «Бета», ИНН 7702222222\n"
                f"3. Груз\n"
                f"Товар, 1 шт\n"
                f"{prefix}Перевозчик\n"
                f"ООО «Гамма»\n"
                f"7. Транспортное средство\n"
                f"А123ВС77\n"
                f"8. Прием груза\n"
                f"г. Москва, склад 10\n"
            )
            sec = split_sections(text)
            assert "carrier" in sec, (
                f"Префикс {prefix!r} помешал распознать Перевозчик"
            )
            assert "Гамма" in sec["carrier"]
