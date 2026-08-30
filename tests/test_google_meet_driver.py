from __future__ import annotations

from ws_collab.drivers.stt.google_meet.driver import assemble_complete_captions


def test_adapter_assembles_only_complete_caption_rows() -> None:
    text, speakers = assemble_complete_captions(
        {
            "partial": {
                "at": 10.0,
                "speaker": "Alice",
                "text": "Here is a screensh.",
                "final": False,
            },
            "complete": {
                "at": 11.0,
                "speaker": "Alice",
                "text": "Here is a screenshot of what I mean.",
                "final": True,
            },
        },
        9.0,
        12.0,
    )

    assert text == "Here is a screenshot of what I mean."
    assert speakers == ["Alice"]
