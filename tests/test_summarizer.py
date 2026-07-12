from pathlib import Path
from typing import BinaryIO, cast

import pytest

from council_meetings import summarizer
from council_meetings.config import settings


class _FakeMessages:
    def __init__(self, recorder: dict[str, object]) -> None:
        self._recorder = recorder

    def create(self, *, model: str, **kwargs: object) -> object:
        self._recorder["model"] = model
        self._recorder.update(kwargs)
        block = type("Block", (), {"text": "a summary"})()
        # Match anthropic.types.TextBlock via isinstance check in summarize_pdf.
        return type("Message", (), {"content": [block]})()


class _FakeFiles:
    def __init__(self, recorder: dict[str, object]) -> None:
        self.recorder = recorder

    def upload(self, *, file: object) -> object:
        self.recorder["uploaded_file"] = file
        self.recorder["uploaded_content"] = cast(BinaryIO, file).read()
        return type("Uploaded", (), {"id": "file_test"})()

    def delete(self, file_id: str) -> None:
        self.recorder["deleted_file_id"] = file_id


class _FakeBeta:
    def __init__(self, recorder: dict[str, object]) -> None:
        self.files = _FakeFiles(recorder)
        self.messages = _FakeMessages(recorder)


class _FakeAnthropic:
    def __init__(self, recorder: dict[str, object]) -> None:
        self.beta = _FakeBeta(recorder)


def test_summarize_pdf_uses_configured_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder: dict[str, object] = {}
    monkeypatch.setattr(settings, "summarization_model", "claude-test-model")
    monkeypatch.setattr(summarizer.anthropic, "Anthropic", lambda **_: _FakeAnthropic(recorder))
    # summarize_pdf validates the returned block with isinstance(TextBlock),
    # so treat our fake block as a TextBlock for the check.
    monkeypatch.setattr(summarizer, "TextBlock", object)

    pdf = tmp_path / "agenda.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    summary = summarizer.summarize_pdf(str(pdf), "agenda")

    assert summary == "a summary"
    assert recorder["model"] == "claude-test-model"
    assert recorder["uploaded_content"] == b"%PDF-1.4 fake"
    assert recorder["deleted_file_id"] == "file_test"
    messages = cast(list[dict[str, object]], recorder["messages"])
    content = cast(list[dict[str, object]], messages[0]["content"])
    source = content[0]["source"]
    assert source == {"type": "file", "file_id": "file_test"}


def test_summarization_model_has_sensible_default() -> None:
    assert settings.summarization_model.startswith("claude-")
