"""列出第 2 个 sheet 的所有内容，找未翻译的英文。"""
from openpyxl import load_workbook

dst = load_workbook("/tmp/dst.xlsx", data_only=False)
src = load_workbook("/tmp/src.xlsx", data_only=False)

# 第 2 个 sheet
for idx in (1,):
    s = src.worksheets[idx]
    d = dst.worksheets[idx]
    print(f"\n=== Sheet#{idx+1}: src='{s.title}' dst='{d.title}' ===")
    for row in s.iter_rows():
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            if cell.data_type == "f":
                continue
            sv = cell.value
            dv = d.cell(row=cell.row, column=cell.column).value
            if sv == dv and any(c.isalpha() and ord(c) < 128 for c in sv):
                # 仍是英文（含字母），未变化
                print(f"  [{cell.coordinate}] 未翻译: {sv!r}")
