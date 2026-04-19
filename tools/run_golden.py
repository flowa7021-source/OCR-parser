# -*- coding: utf-8 -*-
"""Прогон парсера на golden-датасете (inputs/ + expected/).

Сравнивает 9 извлекаемых полей с экспертной разметкой и печатает
per-field accuracy. Запуск:

    python tools/run_golden.py
    python tools/run_golden.py --verbose   # построчно показать misses

В expected/*.json лежит развёрнутая схема (parties/cargo_header/transport/
loading/...). Здесь — лёгкий маппинг к нашим 9 полям.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tn_parser.core import extract_raw_text, parse_text  # noqa: E402
from tn_parser.normalize import normalize_for_sections  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
EXPECTED = ROOT / "expected"


def _load_rows(pdf: Path):
    """Возвращает список ParsedRow.

    Стратегия:
        1) Если рядом с PDF лежит «<stem>.txt» — берём его как сырой
           текст (после OCR-обвязки извне), пропускаем PyMuPDF.
        2) Иначе извлекаем текст через PyMuPDF (работает только на
           PDF с текстовым слоем).
    """
    sidecar = pdf.with_suffix(".txt")
    if sidecar.exists():
        raw = sidecar.read_text(encoding="utf-8")
        return parse_text(normalize_for_sections(raw), pdf.name)
    raw = extract_raw_text(str(pdf))
    if not raw or not raw.strip():
        return None
    return parse_text(raw, pdf.name)

FIELDS = ("number", "date", "shipper", "consignee", "cargo",
          "volume", "driver", "vehicle", "reception")


# --------------------------------------------------------------------------
# Маппинг expected JSON → dict с ожиданиями по нашим полям


def _date_iso_to_ru(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else s


def _first_tn(doc: dict) -> Optional[dict]:
    for d in doc.get("documents", []):
        if d.get("type") == "TN":
            return d
    return None


def _party(tn: dict, role: str) -> Optional[dict]:
    for p in tn.get("parties", []):
        if p.get("role") == role:
            return p
    return None


def expected_fields(expected: dict) -> Dict[str, Any]:
    tn = _first_tn(expected)
    if not tn:
        return {}
    out: Dict[str, Any] = {}
    out["number"] = tn.get("number")
    out["date"] = _date_iso_to_ru(tn.get("date"))
    out["shipper"] = _party(tn, "shipper")
    out["consignee"] = _party(tn, "consignee")
    car = _party(tn, "carrier")
    if car and isinstance(car.get("driver"), dict):
        d = car["driver"]
        out["driver"] = d.get("short_name") or d.get("full_name")
    else:
        out["driver"] = None
    t = tn.get("transport", {}) or {}
    out["vehicle"] = (t.get("vehicle_make"), t.get("vehicle_reg_plate"))
    ch = tn.get("cargo_header", {}) or {}
    out["cargo"] = ch.get("description")
    out["volume"] = (
        ch.get("places_count"),
        ch.get("net_weight_t"),
        ch.get("volume_m3"),
    )
    l = tn.get("loading", {}) or {}
    out["reception"] = l.get("infrastructure_owner")
    return out


# --------------------------------------------------------------------------
# Сравнение


def _norm_az(s: str) -> str:
    return s.replace("А", "A").replace("Е", "E").replace("О", "O").upper()


def check_field(name: str, expected: Any, got: str
                ) -> Tuple[Optional[bool], str]:
    """Возвращает (ok|None, комментарий). None — нет ожидания."""
    if expected in (None, "", [], (None, None), (None, None, None)):
        return None, "no expectation"
    g = (got or "").strip()
    if not g or g == "отсутствует":
        return False, f"MISSING (expected: {expected!r})"

    if name == "number":
        return _norm_az(str(expected)) == _norm_az(g), \
               f"got {g!r}, expected {expected!r}"
    if name == "date":
        return g == expected, f"got {g!r}, expected {expected!r}"

    if name in ("shipper", "consignee"):
        ok = True
        miss = []
        if expected.get("inn") and expected["inn"] not in g:
            ok = False
            miss.append(f"inn {expected['inn']}")
        if expected.get("name"):
            short_name = expected["name"].split(",")[0].strip()
            if short_name.lower() not in g.lower():
                ok = False
                miss.append(f"name «{short_name}»")
        return ok, (f"got {g!r}" if ok
                    else f"got {g!r}, missing: {', '.join(miss)}")

    if name == "driver":
        # Сравнение по фамилии (первое слово) — толерантно к формату
        # «Иванов И.И.» vs «И. И. Иванов».
        e_tokens = re.findall(r"[А-ЯЁ][а-яё]{2,}", str(expected))
        g_tokens = re.findall(r"[А-ЯЁ][а-яё]{2,}", g)
        if not e_tokens:
            return None, f"no surname in expected {expected!r}"
        if not g_tokens:
            return False, f"got {g!r}, no surname found"
        ok = e_tokens[0].lower() == g_tokens[0].lower()
        return ok, f"got {g!r}, expected {expected!r}"

    if name == "vehicle":
        make, plate = expected
        plate_compact = re.sub(r"\s+", "", plate or "").upper()
        g_compact = re.sub(r"\s+", "", g).upper()
        ok_plate = bool(plate_compact) and plate_compact in _norm_az(g_compact)
        ok_make = (not make) or make.upper() in g.upper()
        ok = ok_plate and ok_make
        return ok, f"got {g!r}, expected make={make!r}, plate={plate!r}"

    if name == "cargo":
        words = [w for w in re.findall(r"[А-Яа-яёЁA-Za-z]{4,}", expected.lower())
                 if w not in ("груз", "груза", "масса", "вес", "брутто",
                              "нетто", "объем", "объём")]
        if not words:
            return None, "expected too generic"
        keys = words[:3]
        hits = sum(1 for w in keys if w in g.lower())
        ok = hits >= 1
        return ok, f"got {g!r}; key words {keys}, hits {hits}"

    if name == "volume":
        places, net, vol = expected
        ok = False
        clues = []
        if places is not None and str(places) in g:
            ok, clues = True, clues + [f"places={places}"]
        if vol is not None:
            v_norm = str(vol).replace(".", ",")
            if v_norm in g.replace(".", ","):
                ok, clues = True, clues + [f"vol={vol}"]
        if net is not None:
            n_norm = str(net).replace(".", ",")
            if n_norm in g.replace(".", ","):
                ok, clues = True, clues + [f"net={net}"]
        return ok, f"got {g!r}; matched [{', '.join(clues) or 'none'}]"

    if name == "reception":
        if not isinstance(expected, dict):
            return None, f"unexpected type {type(expected).__name__}"
        miss = []
        if expected.get("inn") and expected["inn"] not in g:
            miss.append(f"inn {expected['inn']}")
        if expected.get("name"):
            short = expected["name"].split(",")[0].strip()
            if short.lower() not in g.lower():
                miss.append(f"name «{short}»")
        ok = not miss
        return ok, (f"got {g!r}" if ok
                    else f"got {g!r}, missing: {', '.join(miss)}")

    return None, "no logic"


# --------------------------------------------------------------------------
# main


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if not INPUTS.exists() or not EXPECTED.exists():
        print(f"Папок {INPUTS}/ и {EXPECTED}/ не существует.", file=sys.stderr)
        return 1

    pdfs = sorted(INPUTS.glob("*.pdf"))
    if not pdfs:
        print(f"В {INPUTS}/ нет PDF.", file=sys.stderr)
        return 1

    totals: Dict[str, Dict[str, int]] = {
        f: {"ok": 0, "fail": 0, "skip": 0} for f in FIELDS
    }

    for pdf in pdfs:
        exp_path = EXPECTED / (pdf.stem + ".json")
        if not exp_path.exists():
            print(f"\n=== {pdf.name} === (нет expected, пропуск)")
            continue
        expected = json.loads(exp_path.read_text(encoding="utf-8"))
        rows = _load_rows(pdf)
        if not rows:
            print(f"\n=== {pdf.name} === нет текстового слоя в PDF "
                  f"и нет sidecar-файла «{pdf.stem}.txt»")
            continue
        row = rows[0]
        exp = expected_fields(expected)
        print(f"\n=== {pdf.name} ===")
        for fld in FIELDS:
            ok, comment = check_field(fld, exp.get(fld), getattr(row, fld))
            mark = "✓" if ok else ("✗" if ok is False else "·")
            if ok is True:
                totals[fld]["ok"] += 1
            elif ok is False:
                totals[fld]["fail"] += 1
            else:
                totals[fld]["skip"] += 1
            if args.verbose or ok is False:
                print(f"  {mark} {fld:10s}  {comment}")
            else:
                print(f"  {mark} {fld:10s}")

    print("\n" + "=" * 60)
    print("ИТОГ:")
    overall_ok = overall_fail = 0
    for fld in FIELDS:
        t = totals[fld]
        ok, fail, skip = t["ok"], t["fail"], t["skip"]
        overall_ok += ok
        overall_fail += fail
        denom = ok + fail
        acc = f"{int(ok / denom * 100)}%" if denom else "—"
        print(f"  {fld:10s}  ok={ok}  fail={fail}  skip={skip}  acc={acc}")
    denom = overall_ok + overall_fail
    print("-" * 60)
    print(f"  {'TOTAL':10s}  ok={overall_ok}  fail={overall_fail}  "
          f"acc={int(overall_ok / denom * 100) if denom else '—'}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
