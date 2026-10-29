"""Synthetic XLSX inputs: no operational records or real worker names."""
import datetime as dt
import io
import struct
import zipfile


def pe_bytes():
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<H", data, 132, 0x8664)
    struct.pack_into("<H", data, 220, 2)
    return bytes(data)


def make_report(date, previous=False, overrides=None):
    values = {"C32": 3 if previous else 5, "D32": 3 if previous else 5,
              "F32": 3 if previous else 4, "C42": 100 if previous else 110,
              "F42": 1000 if previous else 1100, "D20": 77}
    values.update(overrides or {})
    rows = []
    for row in range(1, 61):
        cells = ''.join(f'<c r="{column}{row}"><v>{values.get(column+str(row),1)}</v></c>' for column in 'ABCDEFGHIJKLMNOPQ')
        rows.append(f'<row r="{row}">{cells}</row>')
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    report=f'<worksheet xmlns="{ns}"><sheetData>{"".join(rows)}</sheetData></worksheet>'
    measured=f'<worksheet xmlns="{ns}"><sheetData><row r="3"><c r="A3" t="inlineStr"><is><t>{date.year}년 {date.month}월 {date.day}일</t></is></c></row></sheetData></worksheet>'
    workbook=f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="운영일지" sheetId="1" r:id="rId1"/><sheet name="계측값" sheetId="2" r:id="rId2"/></sheets></workbook>'
    rels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/><Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>'
    files={'xl/workbook.xml':workbook,'xl/_rels/workbook.xml.rels':rels,
           'xl/worksheets/sheet1.xml':report,'xl/worksheets/sheet2.xml':measured,
           'xl/styles.xml':f'<styleSheet xmlns="{ns}"><cellXfs count="1"><xf numFmtId="0"/></cellXfs></styleSheet>'}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr(name,data.encode())
    return out.getvalue()


def replace_parts(data, replacements):
    """Build a synthetic variant without changing any on-disk workbook."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as dest:
        for entry in source.infolist():
            content = replacements.get(entry.filename, source.read(entry.filename))
            dest.writestr(entry, content)
    return out.getvalue()


TODAY=dt.date(2026,10,26)
INPUTS={"B32":"2","G32":"0","D37":"10","E37":"20","F37":"30","F42":"1100"}

WATER_QUALITY={"inlet_ph":"6.8","inlet_temperature":"27.2","outlet_ph":"7.20","outlet_temperature":"26.8"}
