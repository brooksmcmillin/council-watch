import json
import runpy
import sys
from pathlib import Path

import anthropic
import httpx
import pytest
from pypdf import PdfWriter

from council_meetings import summarizer

resource = pytest.importorskip("resource")


@pytest.mark.parametrize("baseline", [False, True])
def test_profiler_uses_offline_transport(
    baseline: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "agenda.pdf"
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.write(path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "profile_pdf_memory.py"
    monkeypatch.setattr(sys, "argv", [str(script), str(path)])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://provider.example/prefix")
    monkeypatch.setattr(resource, "setrlimit", lambda *_: None)
    monkeypatch.setattr(summarizer, "_summarization_client", summarizer._summarization_client)
    monkeypatch.setattr(anthropic, "Anthropic", anthropic.Anthropic)
    monkeypatch.setattr(summarizer.settings, "llm_mode", "gateway")
    monkeypatch.setattr(summarizer.settings, "llm_gateway_url", "")

    def forbid_network(*_: object) -> None:
        pytest.fail("offline profiler tried to use the real HTTP transport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_network)
    if baseline:
        monkeypatch.delattr(summarizer, "_summarization_client")

        def legacy_summarize(pdf_path: str, _doc_type: str) -> str:
            with anthropic.Anthropic(api_key="fixture") as client, Path(pdf_path).open("rb") as fp:
                file_id = client.beta.files.upload(file=fp).id
                client.beta.files.delete(file_id)
            return "offline fixture"

        monkeypatch.setattr(summarizer, "summarize_pdf", legacy_summarize)

    main = runpy.run_path(str(script))["main"]
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["uploads"] == result["deleted"] == 1
    assert result["requests"] == (2 if baseline else 3)
