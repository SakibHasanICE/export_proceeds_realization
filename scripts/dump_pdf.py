"""Show what the extractor sees in a PDF and what the rules produce. Usage: python scripts/dump_pdf.py file.pdf"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.components.ai_agent import read_pdf_text          # noqa: E402
from app.components.bank_parsers import detect_bank, detect_doc_type, parse_with_rules   # noqa: E402

text = read_pdf_text(sys.argv[1])
print("=== TEXT AS THE PARSER SEES IT ===")
print(text)
print("=== DETECTED ===", detect_bank(text), detect_doc_type(text))
print("=== ERP RECORD ===")
print(json.dumps(parse_with_rules(text), indent=2, default=str))
