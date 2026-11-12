from io import BytesIO
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import BinaryIO, cast
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock
from anthropic.types.beta import BetaTextBlock, BetaThinkingBlock
from pypdf import PdfReader, PdfWriter

from council_meetings import summarizer
from council_meetings.config import settings


class _FakeMessages:
    def __init__(self, recorder: dict[str, object]) -> None:
        self._recorder = recorder

    def create(self, *, model: str, **kwargs: object) -> object:
        self._recorder["model"] = model
        self._recorder.update(kwargs)
        return type("Message", (), {"content": [BetaTextBlock(type="text", text="a summary")]})()


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

    def __enter__(self) -> "_FakeAnthropic":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass


def _pdf_bytes(page_count: int) -> bytes:
    writer = PdfWriter()
    for i in range(page_count):
        writer.add_blank_page(width=100 + i, height=100)
    with BytesIO() as output:
        writer.write(output)
        return output.getvalue()


def test_summarize_pdf_uses_configured_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder: dict[str, object] = {}
    monkeypatch.setattr(settings, "summarization_model", "claude-test-model")
    monkeypatch.setattr(summarizer.anthropic, "Anthropic", lambda **_: _FakeAnthropic(recorder))
    pdf = tmp_path / "agenda.pdf"
    data = _pdf_bytes(1)
    pdf.write_bytes(data)

    summary = summarizer.summarize_pdf(str(pdf), "agenda")

    assert summary == "a summary"
    assert recorder["model"] == "claude-test-model"
    assert recorder["uploaded_content"] == data
    assert recorder["deleted_file_id"] == "file_test"
    messages = cast(list[dict[str, object]], recorder["messages"])
    content = cast(list[dict[str, object]], messages[0]["content"])
    source = content[0]["source"]
    assert source == {"type": "file", "file_id": "file_test"}


@pytest.mark.parametrize("beta", [False, True])
def test_response_ignores_thinking_and_joins_text(beta: bool) -> None:
    text_type = BetaTextBlock if beta else TextBlock
    thinking_type = BetaThinkingBlock if beta else ThinkingBlock
    message = SimpleNamespace(
        content=[
            thinking_type(type="thinking", thinking="private reasoning", signature="sig"),
            text_type(type="text", text="First section"),
            text_type(type="text", text="Second section"),
        ]
    )
    assert summarizer._response_text(cast(Message, message)) == "First section\n\nSecond section"


@pytest.mark.parametrize(
    "content",
    [
        [],
        [ThinkingBlock(type="thinking", thinking="private", signature="sig")],
        [TextBlock(type="text", text=" ")],
    ],
)
def test_response_without_text_fails(content: list[object]) -> None:
    with pytest.raises(ValueError, match="contained no text"):
        summarizer._response_text(cast(Message, SimpleNamespace(content=content)))


def test_summarization_model_has_sensible_default() -> None:
    assert settings.summarization_model.startswith("claude-")


def _bad_request(message: str) -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
        body={"error": {"type": "invalid_request_error", "message": message}},
    )


def _mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.beta.files.upload.return_value = SimpleNamespace(id="file_test")
    client.beta.messages.create.return_value = SimpleNamespace(
        content=[BetaTextBlock(type="text", text="PDF summary")]
    )
    client.messages.create.return_value = SimpleNamespace(
        content=[TextBlock(type="text", text="Combined summary")]
    )
    monkeypatch.setattr(summarizer.anthropic, "Anthropic", lambda **_: client)
    return client


@pytest.mark.parametrize("page_count", [5, 601])
def test_pdf_splitting_preserves_all_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    page_count: int,
) -> None:
    client = _mock_client(monkeypatch)
    uploads: list[list[int]] = []

    def upload(*, file: BinaryIO) -> SimpleNamespace:
        reader = PdfReader(file)
        uploads.append([int(page.mediabox.width) for page in reader.pages])
        return SimpleNamespace(id=f"file_{len(uploads)}")

    client.beta.files.upload.side_effect = upload
    if page_count == 5:
        client.beta.messages.create.side_effect = [
            _bad_request("prompt is too long: 1100000 tokens > 1000000 maximum"),
            SimpleNamespace(content=[BetaTextBlock(type="text", text="First part")]),
            SimpleNamespace(content=[BetaTextBlock(type="text", text="Second part")]),
        ]
    path = tmp_path / "meeting.pdf"
    path.write_bytes(_pdf_bytes(page_count))
    assert summarizer.summarize_pdf(str(path), "minutes") == "Combined summary"
    successful_uploads = uploads[1:] if page_count == 5 else uploads
    assert [page for part in successful_uploads for page in part] == list(
        range(100, 100 + page_count)
    )
    assert all(len(part) <= 600 for part in successful_uploads)
    assert client.beta.files.delete.call_count == len(uploads)
    synthesis = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "partial summaries" in synthesis[0]["text"]
    assert "extracted public HTML" not in synthesis[0]["text"]
    if page_count == 5:
        assert "First part" in synthesis[1]["text"]
        assert "Second part" in synthesis[1]["text"]


@pytest.mark.parametrize(
    "message", ["prompt is too long", "A maximum of 600 PDF pages may be provided."]
)
def test_unsplittable_pdf_fails_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
) -> None:
    client = _mock_client(monkeypatch)
    client.beta.messages.create.side_effect = _bad_request(message)
    path = tmp_path / "meeting.pdf"
    path.write_bytes(_pdf_bytes(1))
    with pytest.raises(ValueError, match="single PDF page"):
        summarizer.summarize_pdf(str(path), "agenda")
    client.beta.files.delete.assert_called_once_with("file_test")
    client.messages.create.assert_not_called()


def test_unrelated_pdf_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _mock_client(monkeypatch)
    client.beta.messages.create.side_effect = _bad_request("invalid model")
    path = tmp_path / "meeting.pdf"
    path.write_bytes(_pdf_bytes(2))
    with pytest.raises(anthropic.BadRequestError, match="invalid model"):
        summarizer.summarize_pdf(str(path), "agenda")
    assert client.beta.messages.create.call_count == 1
    client.beta.files.delete.assert_called_once_with("file_test")


def test_oversized_html_text_splits_without_dropping_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client(monkeypatch)
    client.messages.create.side_effect = [
        _bad_request("prompt is too long"),
        SimpleNamespace(content=[TextBlock(type="text", text="First part")]),
        SimpleNamespace(content=[TextBlock(type="text", text="Second part")]),
        SimpleNamespace(content=[TextBlock(type="text", text="Final summary")]),
    ]
    source = "First half. Second half."
    assert summarizer.summarize_text(source, "agenda") == "Final summary"
    calls = client.messages.create.call_args_list
    assert (
        "".join(call.kwargs["messages"][0]["content"][1]["text"] for call in calls[1:3]) == source
    )
    final = calls[-1].kwargs["messages"][0]["content"]
    assert "Linked attachments and staff reports are not included" in final[0]["text"]
    assert "First part" in final[1]["text"] and "Second part" in final[1]["text"]
    client.beta.files.upload.assert_not_called()


@pytest.mark.parametrize("source", ["", "x", "abcdefgh"])
def test_repeated_text_rejection_terminates(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    client = _mock_client(monkeypatch)
    client.messages.create.side_effect = _bad_request("prompt is too long")
    with pytest.raises(anthropic.BadRequestError, match="prompt is too long"):
        summarizer.summarize_text(source, "agenda")
    requests = [
        call.kwargs["messages"][0]["content"][1]["text"]
        for call in client.messages.create.call_args_list
    ]
    assert requests == (["abcdefgh", "abcd", "ab", "a"] if len(source) > 1 else [source])
    client.beta.files.upload.assert_not_called()


def test_text_synthesis_rejection_is_not_recursively_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _mock_client(monkeypatch)
    client.messages.create.side_effect = [
        _bad_request("prompt is too long"),
        SimpleNamespace(content=[TextBlock(type="text", text="First part")]),
        SimpleNamespace(content=[TextBlock(type="text", text="Second part")]),
        _bad_request("prompt is too long"),
    ]
    with pytest.raises(anthropic.BadRequestError, match="prompt is too long"):
        summarizer.summarize_text("source text", "minutes")
    assert client.messages.create.call_count == 4
    client.beta.files.upload.assert_not_called()
