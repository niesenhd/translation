"""Check result docx for footnote content"""
import minio, io, zipfile
from lxml import etree
import sys
sys.path.insert(0, "/app")
from app.core.config import get_settings

s = get_settings()
mc = minio.Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)
obj = mc.get_object(s.minio_bucket, "results/117b4155-769d-4e89-9972-7adb9de6de82.docx")
data = obj.read()
print(f"Result file size: {len(data)} bytes")

z = zipfile.ZipFile(io.BytesIO(data))
parts = [n for n in z.namelist() if "foot" in n or "end" in n]
print(f"Footnote/endnote parts: {parts}")

if "word/footnotes.xml" in z.namelist():
    fn_xml = z.read("word/footnotes.xml")
    root = etree.fromstring(fn_xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    all_t = root.findall(".//w:t", ns)
    total_text = "".join(t.text or "" for t in all_t)
    print(f"Footnotes text chars: {len(total_text)}")
    chinese = sum(1 for c in total_text if ord(c) >= 0x4e00 and ord(c) <= 0x9fff)
    print(f"Chinese chars in footnotes: {chinese}")
    print(f"Preview: {total_text[:400]}")
else:
    print("NO footnotes.xml in result!")
