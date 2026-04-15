# -*- coding: utf-8 -*-
"""Разбиение текста ТН на разделы по их семантической роли.

Формы ТН бывают разных редакций: нумерация разделов гуляет (например, в
форме из ПП №2200 «Перевозчик» — раздел 10, а в старой форме — раздел 6).
Поэтому вместо ключей-номеров мы возвращаем `Dict[role, content]`.

Ключи:
    "head"      — всё до первого опознанного раздела
    "shipper"   — грузоотправитель
    "consignee" — грузополучатель
    "cargo"     — груз
    "carrier"   — перевозчик
    "vehicle"   — транспортное средство
    "reception" — приём груза
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# Канонические начала заголовков. Более длинные/специфичные — первыми,
# чтобы «Приём груза» не перекрывалось «Груз».
#
# Здесь уживаются три формы ТН:
#   - современная, ПП РФ №2200 от 21.12.2020 (основная);
#   - предыдущая, ПП РФ №272 от 15.04.2011;
#   - «Типовая межотраслевая форма 1-Т» (ТТН), постановление Госкомстата
#     №78 от 28.11.1997 — по-прежнему встречается у консервативных
#     перевозчиков и при перевозке алкогольной/с/х продукции. У неё свои
#     названия разделов («Организация-владелец автотранспорта» вместо
#     «Перевозчик», «Пункт погрузки/разгрузки» вместо «Приём/Выдача груза»).
#     Каждый «1-Т»-синоним помечен комментарием «[1-Т]» — чтобы позже
#     легко понять, что удалять, если от формы откажутся.
# ВАЖНО: выбираем только УНИКАЛЬНЫЕ для формы 1-Т фразы, которые
# одновременно НЕ встречаются как ярлыки внутренних полей в современной ТН.
# Поэтому «Водитель», «Автомобиль» отдельно как заголовки НЕ берём —
# в современной ТН они сплошь попадаются как подписи («Водитель: …»,
# «Марка автомобиля»), и мы случайно разрежем раздел «Приём груза».
# ФИО водителя и ГРЗ авто вытащим обычным путём — по шаблонам, а не по
# заголовку раздела.
_ROLE_TITLES: List[Tuple[str, Tuple[str, ...]]] = [
    ("reception", (
        "прием груза", "приём груза", "погрузка груза",
        "пункт погрузки",                      # [1-Т]
        "погрузочно-разгрузочные операции",    # [1-Т]
    )),
    ("consignee", ("грузополучатель",)),
    ("shipper", ("грузоотправитель",)),
    # "средств" (обрезано) покрывает и «средство», и OCR-варианты вроде
    # «средств6», «средствб» — без хвоста «о».
    ("vehicle", (
        "транспортное средств",
        "марка автомобиля",          # [1-Т] — полный ярлык раздела
        "государственный номерной",  # [1-Т] — «Государственный номерной знак»
    )),
    ("carrier", (
        "перевозчик",
        "организация-владелец автотранспорта",  # [1-Т]
        "организация владелец автотранспорта",  # [1-Т] без дефиса (OCR)
        "владелец автотранспорта",              # [1-Т] усечённый вариант
    )),
    ("cargo", (
        "груз",
        "сведения о грузе",        # [1-Т]
        "товарный раздел",         # [1-Т]
    )),
]

# Заголовки, которые мы узнаём как стоп-маркеры, но контент не забираем.
_IGNORED_TITLES: Tuple[str, ...] = (
    "сопроводительные документы",
    "указания грузоотправителя",
    "условия перевозки",
    "информация о принятии",
    "оговорки и замечания",
    "прочие условия",
    "переадресовк",
    "стоимость услуг",
    "стоимость перевозки",
    "дата составления",
    "отметки",
    "выдача груза",
    "сдача груза",
    # [1-Т] специфические «заключительные» разделы — чисто бухгалтерские,
    # данных о ТН не несут. Служат границами для предыдущих ролей.
    "плательщик",
    "заказчик (плательщик)",
    "пункт разгрузки",
    "транспортный раздел",
    "таксировка",
    "расчёт стоимости",
    "расчет стоимости",
    "прилагаемые документы",
    "итого",
    "всего отпущено",
)


# Заголовок с номером: «1.», «2)», «6 .», нестрогий разделитель.
_NUMBERED = re.compile(
    r"(?m)^\s*(\d{1,2})\s*[.)\u00a0]\s*([А-ЯЁа-яё][^\n]{0,80})"
)

# Заголовок без номера — отдельной строкой.
_BARE = re.compile(
    r"(?mi)^\s*("
    + "|".join(
        re.escape(name)
        for _, names in _ROLE_TITLES
        for name in names
    )
    + r"|"
    + "|".join(re.escape(name) for name in _IGNORED_TITLES)
    + r")\b[^\n]{0,80}$"
)

# Кандидат в заголовок с OCR-искажённым префиксом: вместо «1.»/«2)» —
# «&,», «5;», «%.», «| 1.». Если за мусором видна русская фраза длиной
# ≥ 6 букв, _classify_title попробует сопоставить её (в т.ч. fuzzy).
#
# Не ставим диапазоны из [А-Яа-яЁё] напрямую в квантификатор — это
# порождает ложные срабатывания на обычных строках контента.
# Минимум 6 букв на старте отсекает обычные «ООО …», «ИНН …», «АО …».
_HEADER_CANDIDATE = re.compile(
    r"(?m)^"
    # ВАЖНО: только ГОРИЗОНТАЛЬНЫЕ пробелы/табы в классе junk — никаких
    # \s/\n. Иначе класс съедает перевод строки и матч уползает в
    # предыдущую строку («—» / пустую строку перед заголовком), и
    # position маркера становится далеко от реального заголовка.
    r"[ \t|_\-–—=\\/&%§№.,;·*°º‚`'\"‹›«»()\[\]]{0,6}"  # OCR-мусор
    r"(?:\d{1,2}[ \t]*[.)\u00a0:;,]?[ \t]*)?"           # опц. цифра-префикс
    r"([А-ЯЁ][А-Яа-яёЁ][А-Яа-яёЁ\- ]{4,70})"           # русская фраза ≥ 6 букв
)


def _strip_leading_junk(s: str) -> str:
    """Сносит ведущие OCR-символы-мусор («| 1. Грузо…», «&, Перевозчик»)."""
    return re.sub(
        r"^[\s|_\-–—=\\/&%§№.,;·*°º‚`'\"‹›«»()\[\]]+", "", s
    )


def _matches_prefix(text: str, prefix: str) -> bool:
    """`startswith` + проверка границы слова ПОСЛЕ prefix.

    Без границы слова короткий ключ «груз» матчится посреди слова
    «грузоотправитель» (и слова-OCR-искажения вроде «грузоатиравитель»),
    из-за чего раздел получает не ту роль.
    """
    if not text.startswith(prefix):
        return False
    if prefix and prefix[-1].isalpha():
        tail = text[len(prefix):]
        if tail and tail[0].isalpha():
            return False
    return True


def _edit_distance_leq(a: str, b: str, cap: int) -> bool:
    """True, если расстояние Левенштейна между `a` и `b` ≤ cap.

    Ранний выход: как только строка текущей DP-матрицы не содержит
    значений ≤ cap, возвращаем False. На практике для кандидатов
    заголовков (до 20 символов) время микросекунды.
    """
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return False
    # prev[j] = расстояние редактирования между a[:0] и b[:j]
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        best_in_row = curr[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if curr[j] < best_in_row:
                best_in_row = curr[j]
        if best_in_row > cap:
            return False
        prev = curr
    return prev[lb] <= cap


def _fuzzy_match_keyword(low: str, keyword: str, max_dist: int) -> bool:
    """Fuzzy-сопоставление ключевого слова с префиксом строки заголовка.

    Учитываем, что длина OCR-варианта может отличаться от канонической
    на ± max_dist; берём скользящее окно соответствующей длины в начале
    low и меряем расстояние Левенштейна.
    """
    # Слишком короткие ключи не фаззим: риск ложных срабатываний большой.
    if len(keyword) < 8:
        return False
    for delta in range(-max_dist, max_dist + 1):
        win_len = len(keyword) + delta
        if win_len <= 0 or win_len > len(low):
            continue
        window = low[:win_len]
        if _edit_distance_leq(window, keyword, max_dist):
            # После OCR-варианта должна быть не-буква (чтобы не срезать
            # только часть более длинного слова).
            tail = low[win_len:]
            if not tail or not tail[0].isalpha():
                return True
    return False


def _classify_title(title: str) -> Optional[str]:
    """По тексту заголовка определяет роль или "__ignored__".

    Три шага:
      1) Стрипаем ведущий OCR-мусор («|», «&», «%» и т.п.).
      2) Точное сопоставление c проверкой границы слова (чтобы «груз»
         не матчилось посреди «грузоотправитель»).
      3) Fuzzy-сопоставление (Левенштейн ≤ 2) для длинных ключей —
         ловит OCR-искажения вроде «Грузоатиравитель», «Срузосотправитель»,
         «Грузоотиравитель» → shipper; «Пэревозчик», «Лерезозчик» →
         carrier и т.п.
    """
    low = _strip_leading_junk(title).lower().strip()
    if not low:
        return None

    # (2) Точный префикс с word-boundary.
    for ign in _IGNORED_TITLES:
        if _matches_prefix(low, ign):
            return "__ignored__"
    for role, names in _ROLE_TITLES:
        for name in names:
            if _matches_prefix(low, name):
                return role

    # (3) Fuzzy для длинных ключей. Ignored проверяем первыми, чтобы
    # при совпадении с шаблонной фразой (а не данными) не вернуть роль.
    for ign in _IGNORED_TITLES:
        if _fuzzy_match_keyword(low, ign, 2):
            return "__ignored__"
    for role, names in _ROLE_TITLES:
        for name in names:
            if _fuzzy_match_keyword(low, name, 2):
                return role
    return None


def _find_markers(text: str) -> List[Tuple[int, str, str]]:
    """Возвращает отсортированный список (offset, role|"__ignored__", keyword).

    keyword — канонический «префикс», которым роль была распознана в данном
    месте; нужен потом в split_sections, чтобы отрезать ярлык от значения
    на одной строке (например, «Грузоотправитель ООО …» → «ООО …»).

    Стратегия: собираем все возможные маркеры (нумерованные + bare), затем
    для каждой роли оставляем ПЕРВОЕ вхождение. Ignored-маркеры оставляем
    все — они нужны как границы.
    """
    candidates: List[Tuple[int, str, str]] = []

    def _best_keyword(title: str) -> str:
        """Находит самый длинный из известных префиксов, совпавший с title.

        Если точного совпадения нет (OCR-искажение), возвращает тот
        канонический ключ, с которым строка ближе всего по Левенштейну
        (edit distance ≤ 2). Нужен split_sections, чтобы правильно
        отсечь заголовок от inline-значения на одной строке.
        """
        low = _strip_leading_junk(title).lower().strip()
        best = ""
        # Точное совпадение — в приоритете.
        for ign in _IGNORED_TITLES:
            if _matches_prefix(low, ign) and len(ign) > len(best):
                best = ign
        for _role, names in _ROLE_TITLES:
            for name in names:
                if _matches_prefix(low, name) and len(name) > len(best):
                    best = name
        if best:
            return best
        # Fuzzy: выбираем ключ с наименьшим расстоянием редактирования.
        for ign in _IGNORED_TITLES:
            if _fuzzy_match_keyword(low, ign, 2) and len(ign) > len(best):
                best = ign
        for _role, names in _ROLE_TITLES:
            for name in names:
                if _fuzzy_match_keyword(low, name, 2) and len(name) > len(best):
                    best = name
        return best

    # 1) Нумерованные заголовки.
    for m in _NUMBERED.finditer(text):
        role = _classify_title(m.group(2))
        if role is not None:
            candidates.append((m.start(), role, _best_keyword(m.group(2))))

    # 2) Голые заголовки (на отдельной строке).
    for m in _BARE.finditer(text):
        role = _classify_title(m.group(1))
        if role is not None:
            candidates.append((m.start(), role, _best_keyword(m.group(1))))

    # 3) Кандидаты с мусором/OCR-искажением в префиксе. Лоим их fuzzy-
    # сопоставлением, чтобы не потерять секцию из-за замены одной буквы
    # в «Грузоотправитель» или замены «6.» на «&,» в «6. Перевозчик».
    seen_positions: set[int] = {pos for pos, _r, _k in candidates}
    for m in _HEADER_CANDIDATE.finditer(text):
        # Дубликаты с уже найденными — пропускаем.
        title = m.group(1)
        role = _classify_title(title)
        if role is None:
            continue
        # Координата начала _строки_, а не заголовка — так он становится
        # маркером границы раздела (всё, что выше, уходит в предыдущий раздел).
        if m.start() in seen_positions:
            continue
        candidates.append((m.start(), role, _best_keyword(title)))
        seen_positions.add(m.start())

    # Первое вхождение каждой роли.
    seen_roles: set[str] = set()
    markers: List[Tuple[int, str, str]] = []
    # Сортируем сначала по позиции.
    candidates.sort(key=lambda x: x[0])
    for pos, role, kw in candidates:
        if role == "__ignored__":
            markers.append((pos, role, kw))
            continue
        if role in seen_roles:
            continue
        seen_roles.add(role)
        markers.append((pos, role, kw))

    return markers


def split_sections(text: str) -> Dict[str, str]:
    """Делит нормализованный текст на разделы по ролям."""
    if not text:
        return {}

    markers = _find_markers(text)
    result: Dict[str, str] = {}

    if not markers:
        result["head"] = text.strip()
        return result

    head = text[: markers[0][0]].strip()
    if head:
        result["head"] = head

    for i, (pos, role, kw) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        chunk = text[pos:end].strip()

        nl = chunk.find("\n")
        header_line = chunk if nl < 0 else chunk[:nl]
        rest = "" if nl < 0 else chunk[nl + 1 :].strip()

        # Inline-значение в заголовке. Покрываем сразу три формата:
        #   (а) «1. Грузоотправитель: ООО Ромашка»  — двоеточие
        #   (б) «Пункт погрузки   г. Санкт-Петербург»  — 2+ пробелов (форма 1-Т)
        #   (в) «Организация-владелец автотранспорта: ООО …»  — дефис ВНУТРИ
        #       ключевого слова, не разделитель
        # Стратегия: откусываем ровно столько символов, сколько занимает
        # найденный ключ (kw), с возможным числовым префиксом «1.»/«2)».
        # Всё, что осталось, после снятия разделителей — inline.
        inline = ""
        if kw:
            anchor_re = rf"^\s*(?:\d+[.)]\s*)?{re.escape(kw)}"
            m_kw = re.match(anchor_re, header_line, re.IGNORECASE)
            if m_kw:
                tail = header_line[m_kw.end():]
                tail = re.sub(r"^[\s:\-–—]+", "", tail)
                if tail.strip():
                    inline = tail.strip()

        # Если найти по ключу не смогли — пробуем старый разделительный сплит
        # (нужен для «ТРАНСПОРТНАЯ НАКЛАДНАЯ № 123», где kw пустой).
        if not inline:
            inline_split = re.split(r":", header_line, maxsplit=1)
            if len(inline_split) == 2 and inline_split[1].strip():
                inline = inline_split[1].strip()

        if inline and rest:
            body = inline + "\n" + rest
        else:
            body = inline or rest

        if role == "__ignored__":
            continue
        if role not in result:
            result[role] = body

    return result
