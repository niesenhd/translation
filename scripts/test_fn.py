"""Test footnote translation flow"""
import sys
sys.path.insert(0, "/app")
from docx import Document
from lxml import etree

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

        # Serialize back
        new_blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        root2 = etree.fromstring(new_blob)
        paras2 = root2.findall(f"{{{NS_W}}}p")
        for i, p in enumerate(paras2):
            t_nodes = p.findall(f".//{{{NS_W}}}t")
            text = "".join(t.text or "" for t in t_nodes)
            if "TRANSLATED" in text:
                print(f"  Serialized OK: {text[:60]!r}")
                break
        break
