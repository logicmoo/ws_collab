"""Representation conversion service.

A server-side port of the workbench's ``mettaResourceCodec.jsonValueToMetta``
(frontend/src/lib/mettaResourceCodec.ts), so any client -- including the admin
UI -- can ask the server to render an event/value as MeTTa (or pretty JSON)
instead of duplicating the codec on the client. Kept faithful to the TS source
so the MeTTa output matches the workspace viewer exactly.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SAFE_ATOM = re.compile(r'^[^\s(){}";\\]+$')
_TYPED_ATOM = re.compile(r"^(?:true|false|null|-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)$", re.IGNORECASE)
_EMBEDDED = "__metta_json_string_parts__"


def _num(value: Any) -> str:
    """Match JavaScript's String(number): integral floats print without '.0'."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _quote(value: str, force: bool = False) -> str:
    if force:
        return json.dumps(value)
    if _SAFE_ATOM.match(value) and value != "{}" and not _TYPED_ATOM.match(value):
        return value
    return json.dumps(value)


def _single_quote(value: str) -> str:
    return "'" + (
        value.replace("\\", "\\\\").replace("'", "\\'")
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    ) + "'"


def _embedded_json_parts(value: str) -> list[Any] | None:
    parts: list[Any] = []
    cursor = 0
    scan = 0
    found = False
    n = len(value)
    while scan < n:
        if value[scan] not in "{[":
            scan += 1
            continue
        start = scan
        stack: list[str] = []
        quoted = False
        escaped = False
        end = -1
        while scan < n:
            ch = value[scan]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
                scan += 1
                continue
            if ch == '"':
                quoted = True
                scan += 1
                continue
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                opener = stack.pop() if stack else None
                if (opener == "{" and ch != "}") or (opener == "[" and ch != "]"):
                    break
                if not stack:
                    end = scan + 1
                    break
            scan += 1
        if end < 0:
            scan = start + 1
            continue
        try:
            parsed = json.loads(value[start:end])
        except (ValueError, TypeError):
            scan = start + 1
            continue
        if parsed is None or not isinstance(parsed, (dict, list)):
            scan = start + 1
            continue
        if start > cursor:
            parts.append(value[cursor:start])
        parts.append(parsed)
        found = True
        cursor = end
        scan = end
    if not found:
        return None
    if cursor < len(value):
        parts.append(value[cursor:])
    return parts


def _formatted_embedded_string_list_item(value: str) -> list[str] | None:
    parts = _embedded_json_parts(value)
    if parts is None or not any(not isinstance(p, str) for p in parts):
        return None
    lines = [""]
    for part in parts:
        if isinstance(part, str):
            lines[-1] += part
            continue
        pretty = json.dumps(part, indent=2).split("\n")
        lines[-1] += pretty[0]
        lines.extend(pretty[1:])
    return [json.dumps(lines[0])] + [_single_quote(line) for line in lines[1:]]


def _split_long_sentence_lines(value: str, minimum_prefix: int = 50) -> list[str] | None:
    if len(value) <= minimum_prefix or re.search(r"[\r\n]", value):
        return None
    lines: list[str] = []
    remaining = value
    boundary = re.compile(r"[A-Za-z][.!?]\s+")
    while len(remaining) > minimum_prefix:
        split_at = -1
        for match in boundary.finditer(remaining):
            end = match.end()
            if end >= minimum_prefix:
                split_at = end
                break
        if split_at < 0:
            break
        lines.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if not lines:
        return None
    lines.append(remaining)
    return lines


def _formatted_long_sentence_list_item(value: str) -> list[str] | None:
    lines = _split_long_sentence_lines(value)
    if not lines or len(lines) <= 1:
        return None
    return [json.dumps(lines[0])] + [_single_quote(line) for line in lines[1:]]


def json_value_to_metta(value: Any, depth: int = 0, force_quote_string: bool = False) -> str:
    indent = "  " * depth
    child_indent = "  " * (depth + 1)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, str):
        if force_quote_string:
            return _quote(value, True)
        embedded = _embedded_json_parts(value)
        return _quote(value) if embedded is None else json_value_to_metta({_EMBEDDED: embedded}, depth, False)
    if isinstance(value, list):
        if not value:
            return "([])"
        if all(isinstance(it, (int, float)) and not isinstance(it, bool) for it in value):
            return "([] " + " ".join(_num(it) for it in value) + ")"
        quote_string_items = any(isinstance(it, str) and re.search(r"\s", it) for it in value)
        items: list[str] = []
        for it in value:
            if quote_string_items and isinstance(it, str):
                formatted = _formatted_embedded_string_list_item(it)
                if formatted:
                    items.extend(child_indent + line for line in formatted)
                    continue
                wrapped = _formatted_long_sentence_list_item(it)
                if wrapped:
                    items.extend(child_indent + line for line in wrapped)
                    continue
            items.append(child_indent + json_value_to_metta(it, depth + 1, quote_string_items and isinstance(it, str)))
        return "([]\n" + "\n".join(items) + "\n" + indent + ")"
    if isinstance(value, dict):
        entries = list(value.items())
        if not entries:
            return "()"
        items = [
            child_indent + "(" + _quote(str(key)) + " " + json_value_to_metta(item, depth + 1, False) + ")"
            for key, item in entries
        ]
        return "(\n" + "\n".join(items) + "\n" + indent + ")"
    return str(value)


def convert_value(value: Any, to: str = "metta") -> str:
    """Render ``value`` in the requested representation ("metta" or "json")."""

    target = (to or "metta").lower()
    if target == "json":
        return json.dumps(value, indent=2, ensure_ascii=False)
    if target == "metta":
        return json_value_to_metta(value)
    from .errors import ValidationError

    raise ValidationError(f"unsupported conversion target: {to!r}", details={"supported": ["metta", "json"]})
