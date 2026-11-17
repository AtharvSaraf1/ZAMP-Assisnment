"""Gemini reads the invoice PDF (text or scanned image) into InvoiceExtraction.

The model only READS. It never decides anything; decisions come from app/checks.py.
Responses are cached on disk by (model, prompt version, file hash), so re-running the
same file returns the same extraction instantly and does not depend on the network.
"""

import base64
import hashlib
import json
import re
import time

import requests
from pydantic import ValidationError

from app.config import get_settings
from app.models import InvoiceExtraction

PROMPT_VERSION = "v3"
RETRYABLE = {429, 500, 502, 503, 504}

SYSTEM = """You read vendor invoices for an accounts-payable team in India and copy their contents into JSON.
Rules:
- Copy values exactly as printed. Do not calculate, guess or fill in anything that is not on the page.
- If a field is not printed, return null. A missing value is better than an invented one.
- po_reference: only a purchase order number that is explicitly printed (labels like PO, P.O., Purchase Order,
  Order Ref). Never infer it.
- Dates: convert to YYYY-MM-DD. Indian invoices write dates day-first (03/11/2026 is 3 November 2026).
- Numbers: plain numbers without currency symbols or thousands separators (1,38,532.00 -> 138532.0).
- tax_inclusive: true only if the line prices already include GST (e.g. "rates inclusive of GST").
- tax_lines: one entry per tax printed (CGST, SGST, IGST). For a single "GST" amount use tax_type GST.
- subtotal: the pre-tax amount if printed; null otherwise.
- unreadable_notes: list anything you could not read with confidence (smudged, cut off, ambiguous)."""

USER = "Extract this invoice into the JSON schema."


class AiExtractionError(Exception):
    pass


def _gemini_schema(schema: dict, defs: dict | None = None) -> dict:
    """Convert pydantic JSON Schema into the OpenAPI subset Gemini's responseSchema accepts."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return _gemini_schema(defs[schema["$ref"].split("/")[-1]], defs)
    if "anyOf" in schema:
        options = [option for option in schema["anyOf"] if option.get("type") != "null"]
        converted = _gemini_schema(options[0], defs)
        if len(options) < len(schema["anyOf"]):
            converted["nullable"] = True
        return converted
    converted: dict = {"type": schema.get("type", "string").upper()}
    if "description" in schema:
        converted["description"] = schema["description"]
    if "enum" in schema:
        converted["enum"] = schema["enum"]
    if converted["type"] == "OBJECT":
        properties = schema.get("properties", {})
        converted["properties"] = {key: _gemini_schema(value, defs) for key, value in properties.items()}
        converted["required"] = list(properties.keys())
    if converted["type"] == "ARRAY":
        converted["items"] = _gemini_schema(schema["items"], defs)
    return converted


_resolved_model: str | None = None


def _resolve_model(headers: dict) -> str:
    """Use the configured model if this key can call it, otherwise the newest stable '-flash' model."""
    global _resolved_model
    if _resolved_model:
        return _resolved_model
    settings = get_settings()
    response = requests.get(f"{settings.gemini_base_url}/models", headers=headers, params={"pageSize": 200}, timeout=20)
    if response.status_code in (400, 401, 403):
        raise AiExtractionError("the Gemini API key was rejected")
    response.raise_for_status()
    names = [
        model["name"].removeprefix("models/")
        for model in response.json().get("models", [])
        if "generateContent" in model.get("supportedGenerationMethods", [])
    ]
    if settings.gemini_model in names:
        _resolved_model = settings.gemini_model
    else:
        stable = [name for name in names if re.fullmatch(r"gemini-[\d.]+-flash", name)]
        if not stable:
            raise AiExtractionError(f"model {settings.gemini_model} is not available to this key")
        _resolved_model = max(stable, key=lambda name: [int(part) for part in re.findall(r"\d+", name)])
    return _resolved_model


def extract_with_gemini(pdf_bytes: bytes, use_cache: bool = True) -> tuple[InvoiceExtraction, dict]:
    """Return (extraction, info) where info says which model ran and whether the cache was used."""
    settings = get_settings()
    if not settings.ai_configured:
        raise AiExtractionError("no Gemini API key configured")
    headers = {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}
    try:
        model = _resolve_model(headers)
    except requests.RequestException as exc:
        raise AiExtractionError("could not reach the Gemini API") from exc

    cache_key = hashlib.sha256(f"{model}|{PROMPT_VERSION}|".encode() + pdf_bytes).hexdigest()
    cache_file = settings.llm_cache_dir / f"{cache_key}.json"
    if use_cache and cache_file.exists():
        try:
            return InvoiceExtraction.model_validate_json(cache_file.read_text(encoding="utf-8")), {
                "model": model, "cached": True,
            }
        except (OSError, ValidationError):
            cache_file.unlink(missing_ok=True)

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
                {"text": USER},
            ],
        }],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(InvoiceExtraction.model_json_schema()),
        },
    }
    raw = _post(f"{settings.gemini_base_url}/models/{model}:generateContent", headers, body)
    try:
        extraction = InvoiceExtraction.model_validate_json(raw)
    except ValidationError as exc:
        raise AiExtractionError("Gemini returned JSON that does not match the invoice schema") from exc
    cache_file.write_text(extraction.model_dump_json(), encoding="utf-8")
    return extraction, {"model": model, "cached": False}


def _post(url: str, headers: dict, body: dict) -> str:
    for attempt in range(1, 4):
        try:
            response = requests.post(url, headers=headers, data=json.dumps(body), timeout=120)
        except requests.RequestException as exc:
            if attempt == 3:
                raise AiExtractionError("the Gemini request failed (network)") from exc
            time.sleep(2 * attempt)
            continue
        if response.status_code in RETRYABLE and attempt < 3:
            time.sleep(4 * attempt)
            continue
        if not response.ok:
            raise AiExtractionError(f"Gemini returned HTTP {response.status_code}")
        candidates = response.json().get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise AiExtractionError("Gemini returned an empty response")
        return text
    raise AiExtractionError("the Gemini request failed")
