"""Orchestrator: PDF -> text -> rule-based parser -> (fallback) OpenRouter LLM -> ERP record."""
import io
import json
import os

import pdfplumber
import requests
from pypdf import PdfReader

from app.common.logger import get_logger
from app.components.bank_parsers import detect_doc_type, looks_like_export_advice, parse_with_rules
from app.config.settings import settings

log = get_logger(__name__)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

LLM_SCHEMA = {
    "beneficiary": "string (the exporter receiving the advice)",
    "bill_invoice_no": "string (bank's reference for the bill/collection)",
    "received_date": "YYYY-MM-DD (value date of the credit)",
    "currency": "3-letter code of the foreign currency",
    "bill_invoice_amount": "number",
    "distributions": [{"ac_loan_no": "string", "document_currency": "number (credited FCY amount)"}],
}


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((p.extract_text(layout=True) or "") for p in pdf.pages)


def read_pdf_text(path: str) -> str:
    """Text of the PDF; if it is a wrapper with no advice inside, look at embedded PDFs."""
    with open(path, "rb") as f:
        data = f.read()
    text = _pdf_text(data)
    if detect_doc_type(text):
        return text
    try:
        for _name, blobs in PdfReader(io.BytesIO(data)).attachments.items():
            for blob in blobs:
                if blob[:4] == b"%PDF":
                    inner = _pdf_text(blob)
                    if detect_doc_type(inner):
                        return inner
    except Exception:
        pass
    return text


def llm_extract(text: str) -> dict:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    system = (
        "You extract fields from a bank export payment advice. Return ONLY a JSON object "
        f"with exactly this shape: {json.dumps(LLM_SCHEMA)}. Use null when a field is absent. "
        "Never guess. Numbers have no thousands separators."
    )
    resp = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={
            "model": settings.openrouter_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": text[:12000]}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def extract_document(path: str) -> dict:
    text = read_pdf_text(path)
    name = os.path.basename(path)
    rec = parse_with_rules(text)
    if rec is not None:
        rec.update(source_file=name, extractor="rules")
        return rec
    if not looks_like_export_advice(text):
        # e.g. an LC / MT700 document: not for this form, so do not waste an LLM call on it
        return {"source_file": name, "extractor": "none", "doc_type": None, "needs_review": True,
                "issues": ["document type not recognised (not an export payment advice)"]}
    # looks like an export advice from an unknown bank/layout: try the LLM, never trust it blindly
    out = {"source_file": name, "extractor": "llm_fallback", "doc_type": detect_doc_type(text),
           "llm": None, "needs_review": True,
           "issues": ["no rule-based parser for this bank/layout; LLM output unverified"]}
    try:
        out["llm"] = llm_extract(text)
    except Exception as e:   # missing key, network, bad JSON: keep the document, do not retry forever
        log.warning("LLM fallback failed for %s: %s", name, e)
        out["issues"].append(f"LLM fallback failed: {e}")
    return out
