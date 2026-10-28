"""Songdo daily report conversion, preserving the original XLSX package."""
from __future__ import annotations

import datetime as dt
import io
import hashlib
import re
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MANUAL = [
    ("B32", "케이크 반출량", "TON"),
    ("G32", "협잡물 반출량", "TON"),
    ("D37", "100 kW 금일 발전량", ""),
    ("E37", "50 kW 금일 발전량", ""),
    ("F37", "37 kW 금일 발전량", ""),
    ("F42", "시상수 금일지침", "㎥"),
]
CARRY = [
    ("C33", "C32", "케이크 호퍼 A 전일 저장량"),
    ("D33", "D32", "케이크 호퍼 B 전일 저장량"),
    ("F33", "F32", "협잡물 호퍼 전일 저장량"),
    ("D36", "D37", "100 kW 전일 발전량"),
    ("E36", "E37", "50 kW 전일 발전량"),
    ("F36", "F37", "37 kW 전일 발전량"),
    ("B41", "B42", "조경용수 등 전일지침"),
    ("C41", "C42", "분리막 세정수 전일지침"),
    ("D41", "D42", "소포수 전일지침"),
    ("F41", "F42", "시상수 전일지침"),
]
CARRY_DIGITS = {"C33": 2, "D33": 2, "F33": 2, "D36": 3, "E36": 3, "F36": 3,
                "B41": 0, "C41": 0, "D41": 0, "F41": 0}


def excel_precision(value):
    return value.quantize(Decimal(1).scaleb(value.adjusted() - 14), rounding=ROUND_HALF_UP) if value else value


def excel_round(value, places):
    # Excel displays 15 significant digits; remove binary-storage tails before
    # half-up rounding, e.g. 26.074999999999999 -> 26.075 -> 26.08.
    significant = excel_precision(value)
    return significant.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class ReportError(ValueError):
    pass


def number(value, label):
    try:
        if value is None or isinstance(value, bool) or str(value).strip() == "":
            raise InvalidOperation
        result = Decimal(str(value).strip().replace(",", ""))
        if not result.is_finite() or result < 0 or result > Decimal("1e15"):
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise ReportError(f"{label}: 0 이상의 유효한 숫자를 입력해 주세요.") from None


def serial(value):
    return format(value, "f") if isinstance(value, Decimal) else value


class Book:
    def __init__(self, data: bytes, name: str, *, require_report=True):
        self.data, self.name = data, name
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            if sum(x.file_size for x in self.zip.infolist()) > 100_000_000:
                raise ReportError("압축 해제 용량이 너무 큰 엑셀입니다.")
            rels = ET.fromstring(self.zip.read("xl/_rels/workbook.xml.rels"))
            targets = {r.get("Id"): r.get("Target") for r in rels}
            self.workbook_xml = self.zip.read("xl/workbook.xml")
            wb = ET.fromstring(self.workbook_xml)
            self.sheets = {}
            for s in wb.find("s:sheets", NS):
                target = targets[s.get(REL)]
                parts = []
                for part in (target.lstrip("/") if target.startswith("/") else "xl/" + target).split("/"):
                    if part == "..":
                        parts.pop()
                    elif part not in ("", "."):
                        parts.append(part)
                self.sheets[s.get("name")] = "/".join(parts)
            self.shared = []
            if "xl/sharedStrings.xml" in self.zip.namelist():
                for si in ET.fromstring(self.zip.read("xl/sharedStrings.xml")):
                    self.shared.append("".join(t.text or "" for t in si.findall(".//s:t", NS)))
            self.cache = {}
            if require_report and ("운영일지" not in self.sheets or "계측값" not in self.sheets):
                raise ReportError(f"{name}: 운영일지·계측값 시트가 있는 일일 레포트를 선택해 주세요.")
            self.date = self.record_date() if require_report else None
        except ReportError:
            raise
        except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, TypeError):
            raise ReportError(f"{name}: 읽을 수 없는 XLSX 파일입니다.") from None

    def cell(self, address, sheet="운영일지"):
        if sheet not in self.cache:
            root = ET.fromstring(self.zip.read(self.sheets[sheet]))
            self.cache[sheet] = {c.get("r"): c for c in root.findall(".//s:sheetData/s:row/s:c", NS)}
        c = self.cache[sheet].get(address)
        if c is None:
            return None
        if c.get("t") == "inlineStr":
            return "".join(t.text or "" for t in c.findall(".//s:t", NS))
        v = c.find("s:v", NS)
        if v is None or v.text is None:
            return None
        if c.get("t") == "s":
            return self.shared[int(v.text)]
        if c.get("t") in ("e", "str", "b"):
            return v.text
        return Decimal(v.text)

    def numeric(self, address, sheet="운영일지"):
        value = self.cell(address, sheet)
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ReportError(f"{self.name} / {sheet}!{address}: 숫자 또는 수식 계산 결과가 없습니다. Excel에서 계산 후 저장한 파일을 사용해 주세요.")
        return value

    def record_date(self):
        text = str(self.cell("A3", "계측값") or "")
        match = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
        if not match:
            raise ReportError(f"{self.name}: 계측값!A3에서 기록 날짜를 확인할 수 없습니다.")
        try:
            return dt.date(*map(int, match.groups()))
        except ValueError:
            raise ReportError(f"{self.name}: 기록 날짜가 올바르지 않습니다.") from None


def prepare(raw_data, previous_data, raw_name, previous_name, inputs=None):
    raw, prev = Book(raw_data, raw_name), Book(previous_data, previous_name)
    if raw.date - prev.date != dt.timedelta(days=1):
        raise ReportError(f"기록 날짜가 이어지지 않습니다. 원본 {raw.date}, 전날 최종본 {prev.date}. 전날({raw.date - dt.timedelta(days=1)}) 일지를 선택해 주세요.")
    updates, rows = {}, []
    for dest, source, label in CARRY:
        val = excel_round(prev.numeric(source), CARRY_DIGITS[dest])
        updates[dest] = val
        rows.append({"cell": dest, "source": source, "label": label, "value": serial(val)})
    current = {c: raw.numeric(c) for c in ("C32", "D32", "F32", "B43", "C42", "D43")}
    result = {
        "record_date": raw.date.isoformat(), "previous_date": prev.date.isoformat(),
        "output_name": (raw.date + dt.timedelta(days=1)).strftime("%Y%m%d") + ".xlsx",
        "carry": rows,
        "today": {c: serial(v) for c, v in current.items()},
        "warnings": [],
        "source_token": hashlib.sha256(raw_data + previous_data).hexdigest(),
    }
    if inputs is None:
        return result, None, None
    for cell, label, unit in MANUAL:
        updates[cell] = number(inputs.get(cell), label)
    if updates["F42"] < updates["F41"]:
        raise ReportError("시상수 금일지침은 전일지침보다 작을 수 없습니다. 계량기 교체·초기화가 있었다면 별도 보정이 필요합니다.")
    values = {**current, **updates}
    formulas = {
        "A32": ("B32+C32+D32-C33-D33", values["B32"] + values["C32"] + values["D32"] - values["C33"] - values["D33"]),
        "E32": (None, values["F32"] - values["F33"] + values["G32"]),
        "B42": (None, values["B41"] + values["B43"]),
        "D42": (None, values["D41"] + values["D43"]),
        "C43": (None, values["C42"] - values["C41"]),
        "F43": (None, values["F42"] - values["F41"]),
    }
    result["metrics"] = [
        {"label": "케이크 발생량", "value": serial(formulas["A32"][1]), "unit": "TON", "cell": "A32"},
        {"label": "협잡물 발생량", "value": serial(formulas["E32"][1]), "unit": "TON", "cell": "E32"},
        {"label": "분리막 세정수 사용량", "value": serial(formulas["C43"][1]), "unit": "㎥", "cell": "C43"},
        {"label": "시상수 사용량", "value": serial(formulas["F43"][1]), "unit": "㎥", "cell": "F43"},
    ]
    for item in result["metrics"]:
        if Decimal(item["value"]) < 0:
            result["warnings"].append(f"{item['label']}({item['cell']})이 음수입니다. 원본과 전일 값을 확인해 주세요.")
    for addr, (_, val) in formulas.items():
        result.setdefault("computed", {})[addr] = serial(val)
    return result, updates, (raw, formulas)


def patch_cell(xml, address, value, formula=False):
    """Patch one value without rewriting surrounding worksheet XML or styles."""
    text = isinstance(value, str) and formula is False
    payload = ('<is><t xml:space="preserve">' + escape(value) + '</t></is>') if text else '' if value is None else '<v>' + serial(value) + '</v>'
    pattern = re.compile(r'<c\b[^>]*\br="' + address + r'"[^>]*?(?:/>|>.*?</c>)', re.S)
    match = pattern.search(xml)
    if match:
        old = match.group()
        opening = old[:old.index(">") + 1]
        opening = re.sub(r'\s+t="[^"]*"', '', opening).replace("/>", ">")
        if text:
            opening = opening[:-1] + ' t="inlineStr">'
        body = "" if old.endswith("/>") else old[old.index(">") + 1:-4]
        body = re.sub(r'<(?:v|is)\b[^>]*?(?:/>|>.*?</(?:v|is)>)', '', body, flags=re.S)
        if formula is not None:  # False = input; string = new formula; None = keep formula
            body = re.sub(r'<f\b[^>]*?(?:/>|>.*?</f>)', '', body, flags=re.S)
            if formula is not False:
                body = '<f>' + formula + '</f>' + body
        replacement = opening + body + payload + '</c>'
        return xml[:match.start()] + replacement + xml[match.end():]
    rowno = re.search(r"\d+", address).group()
    row = re.search(r'<row\b[^>]*\br="' + rowno + r'"[^>]*>.*?</row>', xml, re.S)
    if not row:
        raise ReportError(f"원본 양식에서 {address} 행을 찾을 수 없습니다.")
    new_cell = f'<c r="{address}"' + (' t="inlineStr"' if text else '') + '>' + (f'<f>{formula}</f>' if formula else '') + payload + '</c>'
    def column(ref):
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group():
            n = n * 26 + ord(ch) - 64
        return n
    body = row.group()
    offset = body.rfind('</row>')
    for c in re.finditer(r'<c\b[^>]*\br="([A-Z]+\d+)"', body):
        if column(c.group(1)) > column(address):
            offset = c.start()
            break
    body = body[:offset] + new_cell + body[offset:]
    return xml[:row.start()] + body + xml[row.end():]


class ReportValues:
    """The current generation's values, shared by both output forms."""
    def __init__(self, raw, updates, formulas):
        self.raw = raw
        self.date = raw.date
        self.values = {**updates, **{cell: value for cell, (_, value) in formulas.items()}}

    def cell(self, address, sheet="운영일지"):
        if sheet == "운영일지" and address in self.values:
            return self.values[address]
        return self.raw.cell(address, sheet)

    def numeric(self, address, sheet="운영일지"):
        if sheet == "운영일지" and address in self.values:
            return self.values[address]
        return self.raw.numeric(address, sheet)


def generate_with_values(raw_data, previous_data, raw_name, previous_name, inputs):
    summary, updates, prepared = prepare(raw_data, previous_data, raw_name, previous_name, inputs)
    raw, formulas = prepared
    path = raw.sheets["운영일지"]
    xml = raw.zip.read(path).decode("utf-8")
    for cell, value in updates.items():
        xml = patch_cell(xml, cell, value)
    for cell, (formula, value) in formulas.items():
        xml = patch_cell(xml, cell, value, formula=formula)
    wbxml = raw.workbook_xml.decode("utf-8")
    calc = re.search(r'<calcPr\b[^>]*?(?:/>|>.*?</calcPr>)', wbxml, re.S)
    newcalc = '<calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
    if calc:
        wbxml = wbxml[:calc.start()] + newcalc + wbxml[calc.end():]
    else:
        wbxml = wbxml.replace('</workbook>', newcalc + '</workbook>')
    # Reject malformed output before anything can be written to disk.
    ET.fromstring(xml)
    ET.fromstring(wbxml)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dest:
        for entry in raw.zip.infolist():
            data = xml.encode("utf-8") if entry.filename == path else wbxml.encode("utf-8") if entry.filename == "xl/workbook.xml" else raw.zip.read(entry.filename)
            dest.writestr(entry, data)
    return out.getvalue(), summary, ReportValues(raw, updates, formulas)


def generate(raw_data, previous_data, raw_name, previous_name, inputs):
    data, summary, _ = generate_with_values(raw_data, previous_data, raw_name, previous_name, inputs)
    return data, summary
