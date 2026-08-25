"""Server-side representation conversion (JSON / MeTTa), ported from the
workbench's mettaResourceCodec so the admin UI can request conversions."""

from __future__ import annotations

import json

from ws_collab.metta_codec import convert_value, json_value_to_metta


def test_metta_object_and_scalars() -> None:
    assert json_value_to_metta({"a": 1, "b": "hi"}) == "(\n  (a 1)\n  (b hi)\n)"
    assert json_value_to_metta({}) == "()"
    assert json_value_to_metta(True) == "true"
    assert json_value_to_metta(None) == "null"


def test_metta_strings_and_arrays() -> None:
    # A string with whitespace is quoted; a safe atom is left bare.
    assert json_value_to_metta({"text": "hello world"}) == '(\n  (text "hello world")\n)'
    assert json_value_to_metta([1, 2, 3]) == "([] 1 2 3)"
    assert json_value_to_metta([]) == "([])"


def test_convert_value_targets() -> None:
    assert convert_value({"a": 1}, "metta") == "(\n  (a 1)\n)"
    assert convert_value({"a": 1}, "json") == json.dumps({"a": 1}, indent=2)


def test_convert_value_rejects_unknown_target() -> None:
    from ws_collab.errors import ValidationError

    try:
        convert_value({}, "yaml")
    except ValidationError:
        return
    raise AssertionError("unknown target must raise ValidationError")
