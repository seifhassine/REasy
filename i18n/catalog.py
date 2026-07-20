"""Validation for Qt Linguist translation catalogs."""

from collections import Counter
from pathlib import Path
import re
from xml.etree import ElementTree


_PLACEHOLDER = re.compile(r"(?<!\{)\{[^{}]*\}(?!\})|%(?:L?\d+|n)")
_HTML_TAG = re.compile(
    r"</?(?:a|b|br|code|em|i|li|p|span|strong|ul)\b[^>]*>", re.IGNORECASE
)
_INCOMPLETE_STATES = {"unfinished", "vanished", "obsolete", "missing"}


class CatalogValidationError(ValueError):
    """Raised when a catalog is incomplete or structurally unsafe."""


def _context_error(context_name: str) -> str | None:
    if not context_name:
        return "catalog contains an empty context"
    if not context_name.lstrip("_")[:1].isupper():
        return f"catalog contains an unstable context: {context_name!r}"
    return None


def _message_errors(context_name: str, message: ElementTree.Element) -> list[str]:
    source = message.findtext("source") or ""
    translation = message.find("translation")
    state = translation.get("type") if translation is not None else "missing"
    translated_raw = "" if translation is None else "".join(translation.itertext())
    translated = translated_raw.strip()
    label = f"{context_name or '<empty>'}: {source!r}"
    if state in _INCOMPLETE_STATES or not translated:
        return [f"incomplete translation ({state or 'empty'}): {label}"]

    errors = []
    if Counter(_PLACEHOLDER.findall(source)) != Counter(_PLACEHOLDER.findall(translated)):
        errors.append(f"placeholder mismatch: {label}")
    if Counter(_HTML_TAG.findall(source)) != Counter(_HTML_TAG.findall(translated)):
        errors.append(f"HTML tag mismatch: {label}")
    if source.count("\n") != translated_raw.count("\n"):
        errors.append(f"newline mismatch: {label}")
    return errors


def validate_catalog(ts_path: Path) -> None:
    root = ElementTree.parse(ts_path).getroot()
    errors = []
    for context in root.findall("context"):
        context_name = (context.findtext("name") or "").strip()
        if error := _context_error(context_name):
            errors.append(error)
        for message in context.findall("message"):
            errors.extend(_message_errors(context_name, message))
    if errors:
        details = "\n  - ".join(errors)
        raise CatalogValidationError(f"Invalid translation catalog {ts_path}:\n  - {details}")
