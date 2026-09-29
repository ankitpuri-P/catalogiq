"""The enrichment schema: what a valid LLM answer looks like, and content keys."""

import hashlib
import json
import re

CATEGORIES = (
    "Groceries",
    "Beverages",
    "Personal Care",
    "Household",
    "Electronics",
    "Fashion",
    "Home & Kitchen",
    "Other",
)

MAX_TITLE_LEN = 300
MAX_BRAND_LEN = 100
MAX_TAGS = 5
MAX_TAG_LEN = 50

_WS = re.compile(r"\s+")


class ValidationError(ValueError):
    """LLM output that does not match the enrichment schema."""


def normalize_content(raw_title, raw_description):
    """raw_title + " " + raw_description, lowercased, whitespace collapsed."""
    text = f"{raw_title or ''} {raw_description or ''}"
    return _WS.sub(" ", text.lower()).strip()


def content_key(raw_title, raw_description):
    """Fixed-size key for the cache table; equal iff normalized content is equal."""
    return hashlib.sha256(normalize_content(raw_title, raw_description).encode()).hexdigest()


def normalize_tags(tags):
    """Lowercase, strip, drop empties and duplicates. Raises on wrong types."""
    if not isinstance(tags, list):
        raise ValidationError("tags must be a list")
    out = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValidationError("every tag must be a string")
        tag = _WS.sub(" ", tag.strip().lower())
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise ValidationError(f"tag longer than {MAX_TAG_LEN} characters")
        if tag not in out:
            out.append(tag)
    return out


def parse_enrichment(text):
    """Parse and validate raw LLM output. Returns a clean dict or raises ValidationError.

    Strict on the things the brief calls out (valid JSON, category in the list),
    lenient on cosmetic issues (tag case, more than 5 tags) that we can fix
    without guessing at meaning.
    """
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"output is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValidationError("output must be a JSON object")

    title = data.get("clean_title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("clean_title must be a non-empty string")
    title = _WS.sub(" ", title).strip()
    if len(title) > MAX_TITLE_LEN:
        raise ValidationError(f"clean_title longer than {MAX_TITLE_LEN} characters")

    category = data.get("category")
    if category not in CATEGORIES:
        raise ValidationError(f"category {category!r} is not one of the allowed categories")

    brand = data.get("brand")
    if brand is not None:
        if not isinstance(brand, str):
            raise ValidationError("brand must be a string or null")
        brand = brand.strip()
        if brand.lower() in ("", "null", "none", "unknown", "n/a"):
            brand = None
        elif len(brand) > MAX_BRAND_LEN:
            raise ValidationError(f"brand longer than {MAX_BRAND_LEN} characters")

    tags = normalize_tags(data.get("tags", []))[:MAX_TAGS]

    return {"clean_title": title, "category": category, "brand": brand, "tags": tags}
