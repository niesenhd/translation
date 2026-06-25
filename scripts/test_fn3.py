"""Check XML structure of footnotes"""
import sys, io
sys.path.insert(0, "/app")
import minio, subprocess
from lxml import etree
from app.core.config import get_settings

s = get_settings()
mc = minio.Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)

obj = mc.get_object(s.minio_bucket, "sources/117b4155-769d-4e89-9972-7adb9de6de82.doc")
data = obj.read()
with open("/tmp/source.doc", "wb") as f:
    f.write(data)
subprocess.run(["libreoffice", "--headless", "--convert-to", "docx", "--outdir", "/tmp", "/tmp/source.doc"],
               capture_output=True, timeout=60)

import zipfile
z = zipfile.ZipFile("/tmp/source.docx")
fn_xml = z.read("word/footnotes.xml")
root = etree.fromstring(fn_xml)

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
print(f"Root tag: {root.tag}")
print(f"Root children: {[c.tag for c in root]}")

# Try direct children
direct_p = root.findall(f"{{{NS_W}}}p")
print(f"Direct w:p: {len(direct_p)}")

# Try footnote children
footnotes = root.findall(f"{{{NS_W}}}footnote")
print(f"w:footnote elements: {len(footnotes)}")
if footnotes:
    fn = footnotes[0]
    print(f"First footnote children: {[c.tag for c in fn]}")
    fn_paras = fn.findall(f"{{{NS_W}}}p")
    print(f"First footnote paragraphs: {len(fn_paras)}")

# Try descendant search
all_p = root.findall(f".//{{{NS_W}}}p")
print(f"All descendant w:p: {len(all_p)}")

# Print first 1000 chars of XML
print(f"\nXML preview:\n{etree.tostring(root, pretty_print=True, encoding='unicode')[:1000]}")
