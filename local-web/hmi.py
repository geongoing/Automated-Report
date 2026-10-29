"""Copy a completed daily report into the existing Songdo HMI form."""
from __future__ import annotations

import io
import datetime as dt
import re
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, DecimalException, InvalidOperation
from report_names import hmi_filename

from report import Book, NS, ReportError, patch_cell, excel_round

HMI_SHEET = "송도지소 운영일지"
# Destination HMI cell -> source 운영일지 cell. Copy numbers, never external links.
CELL_MAP = {}


def row(dest_row, dest_columns, source_row, source_columns):
    for dc, sc in zip(dest_columns, source_columns):
        CELL_MAP[f"{dc}{dest_row}"] = f"{sc}{source_row}"


row(13, "BCDEFGHIJ", 6, "ABCDEFGHI")
row(17, "CDEFGH", 28, "BCDEFG")
row(18, "CDEFGH", 29, "BCDEFG")
CELL_MAP.update({"I17": "H28", "I18": "H29", "J17": "I28"})
row(21, "CDEFGHIJ", 10, "BCDEFGHI")
row(23, "CDEFGHIJ", 12, "BCDEFGHI")
# Only reactors 1, 4, 6, 7, 8 have MLSS readings in this report.
row(25, "CFHIJ", 12, "KLOPQ")
row(29, "CDEFGHIJ", 10, "JKLMNOPQ")
row(30, "CDEFGHIJ", 16, "HIJKLMNO")
row(31, "CDEFGHIJ", 18, "HIJKLMNO")
CELL_MAP["C36"] = "M12"
for dest, source, runtime in [(40, 17, "H"), (41, 18, "I"), (42, 19, "J"),
                               (43, 20, "K"), (44, 21, "L"), (45, 22, "M")]:
    CELL_MAP.update({f"D{dest}": f"E{source}", f"E{dest}": f"F{source}", f"F{dest}": runtime + "21"})
row(40, "IJK", 41, "BCD")
row(41, "IJK", 42, "BCD")
row(42, "IJK", 43, "BCD")
row(48, "BCDEFGH", 32, "ABCDEFG")
row(48, "IJK", 25, "ABC")
row(52, "CDEFG", 37, "BCDEF")

MANUAL_SECTIONS = ["근무자", "분리막 가동상태", "분리막 세정일자", "체류시간", "함수율", "작업현황"]
CELL_DIGITS = {cell: 0 for cell in CELL_MAP}
CELL_DIGITS.update({f"{c}23": 2 for c in "CDEFGHIJ"})
CELL_DIGITS.update({f"{c}48": 2 for c in "BCDEFGH"})
CELL_DIGITS.update({f"E{r}": 3 for r in range(40, 46)})
CELL_DIGITS.update({"D45": 3, "C52": 3, "D52": 3})
FORMAT_OVERRIDES = {**{f"{c}{r}": "#,##0" for c in "CDEFGH" for r in (17, 18)},
                    **{f"E{r}": "auto3" for r in range(40, 46)},
                    "D45": "auto3", "C52": "auto3", "D52": "auto3"}
DAY_CELLS = [f"{c}{r}" for c in "EF" for r in range(2, 6)]
NIGHT_CELLS = ["E6", "F6"]
STATUS_CELLS = [f"{c}28" for c in "CDEFGHIJ"]
DATE_CELLS = [f"{c}{r}" for c in "CDEFGHIJ" for r in (32, 33, 34)]
EDITABLE_CELLS = STATUS_CELLS + DATE_CELLS + ["H52"]
OTHER_CARRY_CELLS = [f"{c}26" for c in "CDEFGHIJ"] + [f"G{r}" for r in range(40, 46)]
WATER_QUALITY_KEYS = ('inlet_ph', 'inlet_temperature', 'outlet_ph', 'outlet_temperature')
WATER_QUALITY_LABELS = dict(zip(WATER_QUALITY_KEYS, ('유입수 pH', '유입수 수온', '방류수 pH', '방류수 수온')))
QUALITY_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'


def water_quality_value(raw, key):
    try:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 30:
            raise InvalidOperation
        value = Decimal(raw.strip())
        if not value.is_finite() or not 0 <= value <= 1000000 or ('ph' in key and value > 14):
            raise InvalidOperation
        return raw.strip()
    except (DecimalException, ValueError):
        raise ReportError(f'{WATER_QUALITY_LABELS[key]}: 유효한 숫자를 입력해 주세요.' + (' pH는 0~14입니다.' if 'ph' in key else ' 수온은 0 이상입니다.')) from None


def validate_water_quality(data):
    if not isinstance(data, dict) or set(data) != set(WATER_QUALITY_KEYS):
        raise ReportError('유입수·방류수의 pH와 수온 4개를 입력해 주세요.')
    return {key: water_quality_value(data[key], key) for key in WATER_QUALITY_KEYS}


def extract_water_quality(text):
    result = dict.fromkeys(WATER_QUALITY_KEYS, '')
    for label, prefix in (('유입수', 'inlet'), ('방류수', 'outlet')):
        match = re.search(label + r'\s*pH\s*[:：]\s*(' + QUALITY_NUMBER + r')\s+수온\s*[:：]\s*(' + QUALITY_NUMBER + ')', text or '', re.IGNORECASE)
        if match:
            for key, value in ((prefix + '_ph', match[1]), (prefix + '_temperature', match[2])):
                try:
                    result[key] = water_quality_value(value, key)
                except ReportError:
                    pass  # Unreadable measurements stay blank, never become zero.
    return result


def compose_h52(previous_text, quality):
    """Replace only the two measurement rows, preserving other work notes."""
    text = previous_text or '○ 소내 순찰 및 점검\n○ 유입수 및 방류수 시료 채수'
    numeric = QUALITY_NUMBER
    for label, prefix in (('유입수', 'inlet'), ('방류수', 'outlet')):
        pattern = re.compile(r'(' + label + r'\s*pH\s*[:：]\s*)' + numeric + r'(\s+수온\s*[:：]\s*)' + numeric, re.IGNORECASE)
        text, count = pattern.subn(lambda match: match[1] + quality[prefix + '_ph'] + match[2] + quality[prefix + '_temperature'], text)
        if not count:
            text = text.rstrip() + f"\n  - {label} pH: {quality[prefix + '_ph']}   수온: {quality[prefix + '_temperature']}℃"
    return text


def hmi_record_date(book, *, reference_date=None):
    if HMI_SHEET not in book.sheets:
        raise ReportError("송도 HMI 운영일지 시트가 없습니다.")
    title = str(book.cell("B8", HMI_SHEET) or "")
    m = re.search(r'근무\s*일지\s*\(\s*(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*\)', title)
    if m:
        return dt.date(*map(int, m.groups()))
    short = re.search(r'근무\s*일지\s*\(\s*(\d{1,2})[.\-/](\d{1,2})\s*\)', title)
    if short and reference_date is not None:
        # Legacy titles omit the year. Accept only the precise date requested
        # by the caller; never use today's year, mtime or a filename guess.
        if tuple(map(int, short.groups())) == (reference_date.month, reference_date.day):
            return reference_date
        raise ReportError("HMI 제목의 월·일이 이월 대상 기록일과 다릅니다.")
    raise ReportError("HMI 제목(B8)에서 기록일을 확인할 수 없습니다. 월·일만 있는 제목은 대상 기록일과 함께 확인해야 합니다.")


def date_epoch(book):
    props = ET.fromstring(book.workbook_xml).find("s:workbookPr", NS)
    return dt.date(1904, 1, 1) if props is not None and props.get("date1904") in ("1", "true") else dt.date(1899, 12, 30)


def date_text(value, book):
    if value is None or value == "":
        return ""
    if isinstance(value, Decimal):
        return (date_epoch(book) + dt.timedelta(days=int(value))).isoformat()
    match = re.fullmatch(r'(20\d{2})\D+(\d{1,2})\D+(\d{1,2})\D*', str(value).strip())
    if match:
        return dt.date(*map(int, match.groups())).isoformat()
    raise ReportError("전날 HMI의 세정일자 형식을 확인해 주세요. 날짜 또는 YYYY-MM-DD 형식을 사용합니다.")


def editable_fields(previous_data=None):
    fields = {cell: "" for cell in EDITABLE_CELLS}
    if previous_data is not None:
        previous = Book(previous_data, "전날 HMI.xlsx", require_report=False)
        for cell in EDITABLE_CELLS:
            value = previous.cell(cell, HMI_SHEET)
            fields[cell] = date_text(value, previous) if cell in DATE_CELLS else str(value) if value is not None else ""
    return fields


def validate_hmi_input(data, record_date):
    if not isinstance(data, dict) or data.get("record_date") != record_date.isoformat():
        raise ReportError("HMI 입력의 기록일이 다릅니다. 해당 기록일의 HMI 이월값을 다시 불러와 주세요.")
    day, night = data.get("day_workers"), data.get("night_workers")
    if not isinstance(day, list) or len(day) > 8 or any(not isinstance(n, str) or not n.strip() or len(n.strip()) > 30 for n in day):
        raise ReportError("일근 근무자는 0~8명까지 입력할 수 있습니다. 이름은 30자 이내로 입력해 주세요.")
    if not isinstance(night, list) or len(night) != 2 or any(not isinstance(n, str) or not n.strip() or len(n.strip()) > 30 for n in night):
        raise ReportError("당직 근무자 2명의 이름을 모두 입력해 주세요.")
    fields = data.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - set(EDITABLE_CELLS):
        raise ReportError("HMI 수정 항목이 올바르지 않습니다.")
    for cell, value in fields.items():
        if not isinstance(value, str) or len(value) > (4000 if cell == "H52" else 100):
            raise ReportError(f"HMI {cell} 입력을 확인해 주세요.")
        if cell in DATE_CELLS and value:
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                raise ReportError(f"HMI {cell} 세정일자를 YYYY-MM-DD 형식으로 입력해 주세요.") from None
    return {"day_workers": [n.strip() for n in day], "night_workers": [n.strip() for n in night], "fields": fields,
            "water_quality": validate_water_quality(data.get('water_quality'))}


def numeric_styles(xml, styles, include_dates=False):
    """Match the PDF's grouping and significant decimals, preserving other style properties."""
    root = ET.fromstring(xml)
    parsed = ET.fromstring(styles)
    numfmts = parsed.find("s:numFmts", NS)
    formats = {x.get("formatCode"): x.get("numFmtId") for x in numfmts} if numfmts is not None else {}
    formats["#,##0"] = "3"
    for code in ["#,##0.000"] + (["yyyy-mm-dd"] if include_dates else []):
        if code in formats:
            continue
        fmt_id = str(max([163] + [int(v) for v in formats.values()]) + 1)
        formats[code] = fmt_id
        section = re.search(r'<numFmts\b[^>]*>(.*?)</numFmts>', styles, re.S)
        if section is None:
            raise ReportError("HMI 양식의 숫자 서식을 읽을 수 없습니다.")
        count = len(re.findall(r'<numFmt\b', section.group(1))) + 1
        opening = re.sub(r'count="\d+"', f'count="{count}"', section.group()[:section.group().index('>') + 1])
        body = opening + section.group(1) + f'<numFmt numFmtId="{fmt_id}" formatCode="{code}"/></numFmts>'
        styles = styles[:section.start()] + body + styles[section.end():]
    section = re.search(r'<cellXfs\b[^>]*>(.*?)</cellXfs>', styles, re.S)
    tokens = re.findall(r'<xf\b[^>]*?(?:/>|>.*?</xf>)', section.group(1), re.S)
    additions, clones = [], {}
    overrides = {**FORMAT_OVERRIDES, **({c: "yyyy-mm-dd" for c in DATE_CELLS} if include_dates else {})}
    for cell, format_code in overrides.items():
        element = root.find(f'.//s:c[@r="{cell}"]', NS)
        if format_code == "auto3":
            value = Decimal(element.find("s:v", NS).text)
            format_code = "#,##0" if value == value.to_integral_value() else "#,##0.000"
        source_id, fmt_id = int(element.get("s", "0")), formats[format_code]
        key = source_id, fmt_id
        if key not in clones:
            clone = re.sub(r'numFmtId="\d+"', f'numFmtId="{fmt_id}"', tokens[source_id], count=1)
            clones[key] = len(tokens) + len(additions)
            additions.append(clone)
        xml = re.sub(r'(<c\b[^>]*\br="' + cell + r'"[^>]*\bs=")\d+("[^>]*>)', rf'\g<1>{clones[key]}\2', xml, count=1)
    opening = re.sub(r'count="\d+"', f'count="{len(tokens) + len(additions)}"', section.group()[:section.group().index('>') + 1])
    replacement = opening + section.group(1) + ''.join(additions) + '</cellXfs>'
    styles = styles[:section.start()] + replacement + styles[section.end():]
    return xml, styles


def generate_hmi(final_data, template_data, final_name="최종일지.xlsx", *, previous_data=None, user_input=None):
    return render_hmi(Book(final_data, final_name), template_data, previous_data=previous_data, user_input=user_input)


def render_hmi(final, template_data, *, previous_data=None, user_input=None):
    template = Book(template_data, "송도_HMI.xlsx", require_report=False)
    if HMI_SHEET not in template.sheets:
        raise ReportError("송도_HMI.xlsx에 ‘송도지소 운영일지’ 시트가 없습니다.")
    sheet_path = template.sheets[HMI_SHEET]
    xml = template.zip.read(sheet_path).decode("utf-8")
    required_labels = {"B11": "유입하수량", "B30": "차압", "B47": "케이크", "C40": "A 호기", "C42": "A 호기"}
    for cell, label in required_labels.items():
        if label not in str(template.cell(cell, HMI_SHEET) or ""):
            raise ReportError(f"HMI 양식 {cell}의 항목이 달라졌습니다. 송도_HMI.xlsx 양식을 확인해 주세요.")
    mapped = []
    for dest, source in CELL_MAP.items():
        value = final.cell(source)
        # Empty export quantities are intentionally blank in the reference.
        if value is None and source != "G32":
            final.numeric(source)  # Gives a precise cell-specific error.
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
            final.numeric(source)
        if value is not None:
            value = excel_round(value, CELL_DIGITS[dest])
        xml = patch_cell(xml, dest, value)
        mapped.append({"target": dest, "source": source, "value": str(value) if value is not None else None})
    xml = patch_cell(xml, "B8", "송도지소 중앙 제어실 근무 일지(" + final.date.strftime("%Y.%m.%d") + ")")
    staff = None
    if user_input is not None:
        staff = validate_hmi_input(user_input, final.date)
        fields = editable_fields(previous_data)
        fields.update(staff["fields"])
        fields['H52'] = compose_h52(fields.get('H52'), staff['water_quality'])
        if previous_data is not None:
            previous = Book(previous_data, "전날 HMI.xlsx", require_report=False)
            if hmi_record_date(previous, reference_date=final.date - dt.timedelta(days=1)) != final.date - dt.timedelta(days=1):
                raise ReportError("HMI 이월 원본은 정확히 하루 전 기록일이어야 합니다.")
            for cell in OTHER_CARRY_CELLS:
                xml = patch_cell(xml, cell, previous.cell(cell, HMI_SHEET))
        for cell, value in fields.items():
            if cell in DATE_CELLS:
                value = Decimal((dt.date.fromisoformat(value) - date_epoch(template)).days) if value else None
            xml = patch_cell(xml, cell, value if value != "" else None)
        # Always replace every worker slot; never carry yesterday's staff.
        for cells, names in [(DAY_CELLS, staff["day_workers"]), (NIGHT_CELLS, staff["night_workers"])]:
            for index, cell in enumerate(cells):
                xml = patch_cell(xml, cell, names[index] if index < len(names) else None)
    styles = template.zip.read("xl/styles.xml").decode("utf-8")
    xml, styles = numeric_styles(xml, styles, include_dates=staff is not None)
    ET.fromstring(xml)
    ET.fromstring(styles)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as dest:
        for entry in template.zip.infolist():
            content = xml.encode("utf-8") if entry.filename == sheet_path else styles.encode("utf-8") if entry.filename == "xl/styles.xml" else template.zip.read(entry.filename)
            dest.writestr(entry, content)
    return out.getvalue(), {"output_name": hmi_filename(final.date),
                            "record_date": final.date.isoformat(), "mapped_count": len(mapped) + 1,
                            "mapping": mapped, "manual_sections": MANUAL_SECTIONS,
                            "day_count": len(staff["day_workers"]) if staff else None,
                            "night_count": len(staff["night_workers"]) if staff else None,
                            "note": "전날 HMI의 가동상태·세정일자·작업내용에 웹 수정값을 반영하고, 당일 근무자를 입력했습니다." if staff and previous_data else "전날 HMI가 없어 웹에서 입력한 초기값과 근무자를 반영했습니다." if staff else "근무자·분리막 가동상태·세정일자·체류시간·함수율·작업현황은 최종 일지에 없어 HMI 양식의 값을 유지합니다. 생성한 HMI 엑셀에서 확인·작성해 주세요."}
