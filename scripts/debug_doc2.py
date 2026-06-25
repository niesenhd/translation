"""Check for hidden content in text boxes, shapes, etc."""
import zipfile
from lxml import etree

docx_path = "/tmp/source.docx"
z = zipfile.ZipFile(docx_path)

# List all parts
print("=== Parts in docx ===")
for name in z.namelist():
    info = z.getinfo(name)
    print(f"  {name}: {info.file_size} bytes")

# Check document.xml for text content
doc_xml = z.read("word/document.xml")
root = etree.fromstring(doc_xml)

# Count all text nodes
ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
all_t = root.findall(".//w:t", ns)
total_text = "".join(t.text or "" for t in all_t)
print(f"\nTotal text in document.xml: {len(total_text)} chars")

# Check for text boxes (w:txbxContent)
txbx = root.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent")
print(f"Text boxes (w:txbxContent): {len(txbx)}")
for i, tb in enumerate(txbx):
    tb_text = "".join(t.text or "" for t in tb.findall(".//w:t", ns))
    print(f"  Text box {i}: {len(tb_text)} chars")
    if tb_text:
        print(f"    Preview: {tb_text[:200]}")

# Check for drawing elements
drawings = root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor")
print(f"\nDrawing anchors: {len(drawings)}")
drawings2 = root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}inline")
print(f"Drawing inline: {len(drawings2)}")

# Check footnotes
if "word/footnotes.xml" in z.namelist():
    fn_xml = z.read("word/footnotes.xml")
    fn_root = etree.fromstring(fn_xml)
    fn_t = fn_root.findall(".//w:t", ns)
    fn_text = "".join(t.text or "" for t in fn_t)
    print(f"\nFootnotes text: {len(fn_text)} chars")
    if fn_text:
        print(f"  Preview: {fn_text[:200]}")

# Check endnotes
if "word/endnotes.xml" in z.namelist():
    en_xml = z.read("word/endnotes.xml")
    en_root = etree.fromstring(en_xml)
    en_t = en_root.findall(".//w:t", ns)
    en_text = "".join(t.text or "" for t in en_t)
    print(f"\nEndnotes text: {len(en_text)} chars")

# Count paragraphs in body only
body_paras = root.findall(".//w:body/w:p", ns)
print(f"\nParagraphs directly in body: {len(body_paras)}")

# Count ALL paragraphs anywhere
all_paras = root.findall(".//w:p", ns)
print(f"All paragraphs (anywhere): {len(all_paras)}")
