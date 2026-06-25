"""Inspect footnotes part structure"""
import sys
sys.path.insert(0, "/app")
from docx import Document
import minio
from app.core.config import get_settings

s = get_settings()
mc = minio.Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False)

# Use the already-converted docx
doc = Document("/tmp/source.docx")

part = doc.part
for rel in part.rels.values():
    if rel.reltype.endswith("/footnotes"):
        fn_part = rel.target_part
        print(f"fn_part type: {type(fn_part)}")
        print(f"fn_part dir: {[a for a in dir(fn_part) if not a.startswith('_')]}")
        # Try different attribute names
        for attr in ("element", "_element", "xml", "blob", "raw_element", "part"):
            if hasattr(fn_part, attr):
                val = getattr(fn_part, attr)
                print(f"  has {attr}: type={type(val)}")
        # Check if it has .rels or other
        print(f"  target_ref: {fn_part.partname}")
