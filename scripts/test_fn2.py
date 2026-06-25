"""Test footnote translation - download from MinIO"""
import sys, io
sys.path.insert(0, "/app")
import minio
from docx import Document
from lxml import etree
from app.core.config import get_settings

s = get_settings()
mc = minio.Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)

# Download source
obj = mc.get_object(s.minio_bucket, "sources/117b4155-769d-4e89-9972-7adb9de6de82.doc")
data = obj.read()
with open("/tmp/source.doc", "wb") as f:
    f.write(data)

# Convert to docx
import subprocess
subprocess.run(["libreoffice", "--headless", "--convert-to", "docx", "--outdir", "/tmp", "/tmp/source.doc"],
               capture_output=True, timeout=60)

doc = Document("/tmp/source.docx")
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

part = doc.part
for rel in part.rels.values():
    if rel.reltype.endswith("/footnotes"):
        fn_part = rel.target_part
        root = etree.fromstring(fn_part.blob)
        paras = root.findall(f"{{{NS_W}}}p")
        print(f"Found {len(paras)} footnote paragraphs")

        # Test modifying first non-empty paragraph
        for i, p in enumerate(paras):
            t_nodes = p.findall(f".//{{{NS_W}}}t")
            text = "".join(t.text or "" for t in t_nodes)
            if text.strip():
                print(f"  Para {i}: {len(t_nodes)} t-nodes, text[:60]={text[:60]!r}")
                # Modify first t node
                t_nodes[0].text = "TRANSLATED_TEXT_TEST"
                for t in t_nodes[1:]:
                    t.text = ""
                print(f"  After set: t_nodes[0].text={t_nodes[0].text!r}")

                # Verify the tree was modified
                t_nodes2 = p.findall(f".//{{{NS_W}}}t")
                verify = "".join(t.text or "" for t in t_nodes2)
                print(f"  Verify: {verify[:60]!r}")
                break

        # Serialize back to blob
        new_blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        fn_part._blob = new_blob

        # Save docx
        out = io.BytesIO()
        doc.save(out)
        out.seek(0)

        # Verify in saved file
        import zipfile
        z = zipfile.ZipFile(out)
        fn_xml = z.read("word/footnotes.xml")
        root2 = etree.fromstring(fn_xml)
        paras2 = root2.findall(f"{{{NS_W}}}p")
        found = False
        for p in paras2:
            t_nodes = p.findall(f".//{{{NS_W}}}t")
            text = "".join(t.text or "" for t in t_nodes)
            if "TRANSLATED" in text:
                print(f"  SAVED OK: {text[:60]!r}")
                found = True
                break
        if not found:
            print("  FAILED: translated text not found in saved file!")
        break
