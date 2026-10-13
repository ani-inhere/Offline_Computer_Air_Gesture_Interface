"""Live Word integration test (Windows + Microsoft Word). Opt-in: ``pytest -m word_live``.

Starts its own hidden Word instance (never touches a Word you have open),
creates a document, inserts a snippet, exports a PDF into a temp allowed
folder, closes the document and lets the adapter quit Word.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from helpers import make_request

from agc.engine.automation import ActionDispatcher, ActionType, Status, Tier
from agc.engine.automation.adapters.word_com import WordAdapter, WordConfig

pytestmark = [
    pytest.mark.word_live,
    pytest.mark.skipif(sys.platform != "win32", reason="Word COM is Windows-only"),
]


def test_word_creates_doc_inserts_snippet_and_exports_pdf(tmp_path, collector):
    out_dir = tmp_path / "AGC"
    snippets = tmp_path / "snippets.yaml"
    snippets.write_text("snippets:\n  meeting_header: \"Meeting notes - {date}\\n\"\n", encoding="utf-8")
    adapter = WordAdapter(WordConfig(allowed_output_dir=out_dir, snippets_path=snippets,
                                     attach_to_running=False, visible=False, allow_close=True))
    available, reason = adapter.is_available()
    if not available:
        pytest.skip(reason)

    dispatcher = ActionDispatcher()
    dispatcher.set_result_callback(collector)
    dispatcher.register_backend(Tier.APP_API, adapter)
    dispatcher.start()
    try:
        steps = [
            ("new_document", {}),
            ("insert_text", {"snippet_id": "meeting_header"}),
            ("export_pdf", {"name_template": "agc_live_{timestamp}"}),
            ("close_document", {"name_template": "agc_live_doc"}),
        ]
        ids = [dispatcher.submit(make_request(op, type=ActionType.APP_API, app="word", args=args, timeout_s=60))
               for op, args in steps]
        results = collector.wait_all(ids, timeout=120)
        for (op, _), result in zip(steps, results):
            assert result.status is Status.OK, f"{op}: {result.message}"
    finally:
        assert dispatcher.shutdown(timeout=15)

    pdf = Path(results[2].data["path"])
    assert pdf.parent == out_dir.resolve()
    assert pdf.exists() and pdf.stat().st_size > 0
    assert pdf.read_bytes()[:5] == b"%PDF-"
    assert Path(results[3].data["saved_to"]).suffix == ".docx"
