"""直接在容器内调用引擎翻译 xlsx，对比翻译效果。"""
import io
from openpyxl import load_workbook
from app.services.document_engine import translate_xlsx, TranslationContext
from app.services.translator import get_translator

with open("/tmp/src.xlsx", "rb") as f:
    src_data = f.read()

ctx = TranslationContext(target_lang="zh", source_lang="auto", output_mode="plain")
translator = get_translator()

print("开始翻译...")
out = translate_xlsx(src_data, translator, ctx)
with open("/tmp/dst_new.xlsx", "wb") as f:
    f.write(out)
print(f"完成，输出大小: {len(out)} 字节")

# 对比第 2 页
src = load_workbook("/tmp/src.xlsx", data_only=False)
dst = load_workbook("/tmp/dst_new.xlsx", data_only=False)

for idx in range(len(src.worksheets)):
    s = src.worksheets[idx]
    d = dst.worksheets[idx]
    print(f"\n=== Sheet#{idx+1}: '{s.title}' -> '{d.title}' ===")
    untranslated_en = []
    translated = []
    for row in s.iter_rows():
        for cell in row:
            if not isinstance(cell.value, str) or cell.data_type == "f":
                continue
            sv = cell.value
            dv = d.cell(row=cell.row, column=cell.column).value
            has_en = any(c.isalpha() and ord(c) < 128 for c in sv)
            if sv == dv and has_en:
                untranslated_en.append((cell.coordinate, sv))
            elif sv != dv:
                translated.append((cell.coordinate, sv, dv))
    print(f"  已翻译: {len(translated)} 条")
    for c, s_v, d_v in translated[:5]:
        print(f"    [{c}] {s_v!r} -> {d_v!r}")
    if len(translated) > 5:
        print(f"    ... 还有 {len(translated)-5} 条")
    print(f"  仍是英文（保持原文）: {len(untranslated_en)} 条")
    for c, sv in untranslated_en[:10]:
        print(f"    [{c}] {sv!r}")
    if len(untranslated_en) > 10:
        print(f"    ... 还有 {len(untranslated_en)-10} 条")
