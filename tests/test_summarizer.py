from io import BufferedReader, BytesIO
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import BinaryIO, cast
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaThinkingBlock
from pydantic import SecretStr
from pypdf import PdfReader, PdfWriter

from council_meetings import summarizer
from council_meetings.config import Settings, settings


@pytest.fixture(autouse=True)
def _direct_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_mode", "direct")
    monkeypatch.setattr(settings, "anthropic_api_key", "direct-fixture")


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
        return type("Uploaded", (), {"id": "opaque-test-id"})()

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
    assert recorder["deleted_file_id"] == "opaque-test-id"
    messages = cast(list[dict[str, object]], recorder["messages"])
    content = cast(list[dict[str, object]], messages[0]["content"])
    source = content[0]["source"]
    assert source == {"type": "file", "file_id": "opaque-test-id"}


@pytest.mark.parametrize("gateway_status", [200, 401, 307])
def test_native_gateway_pdf_and_text_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gateway_status: int
) -> None:
    monkeypatch.setenv("LLM_MODE", "gateway")
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/anthropic/")
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "project-fixture")
    if gateway_status == 200:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-fixture")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong-proxy.example:8080")
    monkeypatch.setenv("https_proxy", "http://wrong-proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent-fixture-ca")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-provider-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://wrong.example")
    monkeypatch.setattr(summarizer, "settings", Settings(_env_file=None))
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gateway.example"
        assert request.headers["x-api-key"] == "project-fixture"
        assert "authorization" not in request.headers
        path = request.url.path
        requests.append((request.method, path))
        body = request.read()
        if path.endswith("/files"):
            assert b"%PDF-" in body
            return httpx.Response(200, json={"id": "file_fixture"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "file_fixture", "type": "file_deleted"})
        assert path == "/anthropic/v1/messages"
        if "beta=true" in str(request.url):
            assert b'"file_id":"file_fixture"' in body
        else:
            assert b"public agenda text" in body
        if gateway_status == 401:
            return httpx.Response(401, json={"error": {"message": "invalid project key"}})
        if gateway_status == 307:
            return httpx.Response(307, headers={"Location": "https://wrong.example"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "summary"}]})

    transport = httpx.MockTransport(respond)

    def send(selected: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        assert isinstance(selected, summarizer._GatewayTransport)
        return transport.handle_request(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    pdf = tmp_path / "agenda.pdf"
    pdf.write_bytes(_pdf_bytes(1))
    if gateway_status != 200:
        with pytest.raises(anthropic.APIStatusError) as pdf_error:
            summarizer.summarize_pdf(str(pdf), "agenda")
        with pytest.raises(anthropic.APIStatusError) as text_error:
            summarizer.summarize_text("public agenda text", "agenda")
        assert pdf_error.value.status_code == text_error.value.status_code == gateway_status
    else:
        assert summarizer.summarize_pdf(str(pdf), "agenda") == "summary"
        assert summarizer.summarize_text("public agenda text", "agenda") == "summary"
    assert requests == [
        ("POST", "/anthropic/v1/files"),
        ("POST", "/anthropic/v1/messages"),
        ("DELETE", "/anthropic/v1/files/file_fixture"),
        ("POST", "/anthropic/v1/messages"),
    ]


@pytest.mark.parametrize(
    ("url", "key", "error"),
    [
        ("https://gateway.example/anthropic", "  ", "requires LLM_GATEWAY_URL"),
        ("", "project-fixture", "requires LLM_GATEWAY_URL"),
        ("http://gateway.example/anthropic", "project-fixture", "valid HTTPS base URL"),
        ("https://user@gateway.example/anthropic", "project-fixture", "valid HTTPS base URL"),
        ("https:///anthropic", "project-fixture", "valid HTTPS base URL"),
        ("https://gateway.example:bad/anthropic", "project-fixture", "valid HTTPS base URL"),
        ("https://gateway.example/anthropic?token=x", "project-fixture", "valid HTTPS base URL"),
        ("https://gateway.example/anthropic#part", "project-fixture", "valid HTTPS base URL"),
        ("https://gateway.example", "project-fixture", "must end in /anthropic"),
        ("https://gateway.example/anthropic/v1", "project-fixture", "must end in /anthropic"),
        ("https://gateway.example/openai", "project-fixture", "must end in /anthropic"),
        ("https://gateway.example:0/anthropic", "project-fixture", "valid HTTPS base URL"),
        ("https://gateway.example:65536/anthropic", "project-fixture", "valid HTTPS base URL"),
    ],
)
def test_invalid_gateway_configuration_does_not_use_provider_key(
    monkeypatch: pytest.MonkeyPatch, url: str, key: str, error: str
) -> None:
    monkeypatch.setattr(settings, "llm_mode", "gateway")
    monkeypatch.setattr(settings, "llm_gateway_url", url)
    monkeypatch.setattr(settings, "llm_gateway_api_key", SecretStr(key))
    client = MagicMock()
    monkeypatch.setattr(anthropic, "Anthropic", client)
    with pytest.raises(ValueError, match=error):
        summarizer.summarize_unsummarized()
    with pytest.raises(ValueError, match=error):
        summarizer.summarize_text("public agenda text", "agenda")
    client.assert_not_called()


@pytest.mark.parametrize("mode", [None, "direct"])
def test_direct_mode_keeps_sdk_endpoint_resolution(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is None:
        monkeypatch.delenv("LLM_MODE", raising=False)
    else:
        monkeypatch.setenv("LLM_MODE", mode)
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/anthropic")
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "project-fixture")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-fixture")
    for override, expected in [
        (None, "https://api.anthropic.com"),
        ("https://provider.example", "https://provider.example"),
    ]:
        if override is None:
            monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", override)
        monkeypatch.setattr(summarizer, "settings", Settings(_env_file=None))
        with summarizer._summarization_client() as client:
            assert str(client.base_url).rstrip("/") == expected
            assert client.api_key == "direct-fixture"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(summarizer, "settings", Settings(_env_file=None))
    client_factory = MagicMock()
    monkeypatch.setattr(anthropic, "Anthropic", client_factory)
    assert summarizer.summarize_unsummarized() == 0
    client_factory.assert_not_called()


@pytest.mark.parametrize("file_id", ["file_invalid/path", "file_" + "a" * 252])
def test_pdf_rejects_invalid_upload_id_without_followup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, file_id: str
) -> None:
    client = _mock_client(monkeypatch)
    client.beta.files.upload.return_value = SimpleNamespace(id=file_id)
    pdf = tmp_path / "agenda.pdf"
    pdf.write_bytes(_pdf_bytes(1))
    with pytest.raises(ValueError, match="invalid file ID"):
        summarizer.summarize_pdf(str(pdf), "agenda")
    client.beta.messages.create.assert_not_called()
    client.beta.files.delete.assert_not_called()


def test_pdf_uses_disk_handles_without_whole_file_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _mock_client(monkeypatch)
    path = tmp_path / "meeting.pdf"
    path.write_bytes(_pdf_bytes(2))
    real_reader = summarizer.PdfReader

    def reader(source: BinaryIO) -> PdfReader:
        assert isinstance(source, BufferedReader)
        return real_reader(source)

    def upload(*, file: BinaryIO) -> SimpleNamespace:
        assert isinstance(file, BufferedReader)
        assert file.read(5) == b"%PDF-"
        return SimpleNamespace(id="file_streamed")

    def no_read_bytes(_path: Path) -> bytes:
        raise AssertionError("PDFs must not be read wholesale")

    monkeypatch.setattr(summarizer, "PdfReader", reader)
    monkeypatch.setattr(Path, "read_bytes", no_read_bytes)
    client.beta.files.upload.side_effect = upload
    assert summarizer.summarize_pdf(str(path), "agenda") == "PDF summary"
    client.beta.files.delete.assert_called_once_with("file_streamed")


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


@pytest.mark.parametrize("beta", [False, True])
@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "end_turn", "tool_use"])
def test_empty_response_logs_only_diagnostic_metadata(
    beta: bool, stop_reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    message_type = BetaMessage if beta else Message
    message = message_type.model_validate(
        {
            "id": "private-message-id",
            "type": "message",
            "role": "assistant",
            "model": "private-model-field",
            "stop_reason": stop_reason,
            "stop_sequence": "private-stop-sequence",
            "content": [
                {"type": "thinking", "thinking": "private-thinking", "signature": "private-sig"},
                {
                    "type": "tool_use",
                    "id": "private-tool-id",
                    "name": "private-tool-name",
                    "input": {"payload": "private-tool-input"},
                },
                {"type": "text", "text": " "},
            ],
            "usage": {"input_tokens": 321, "output_tokens": 2048},
            "extra": "private-extra-field",
            "stop_details": {
                "category": "general_harms",
                "explanation": "private-refusal-explanation",
                "recommended_model": "private-recommended-model",
            },
        }
    )

    expected_error = summarizer.SummaryRefusedError if stop_reason == "refusal" else ValueError
    with pytest.raises(expected_error, match="^Summarization response contained no text$") as error:
        summarizer._response_text(message)
    assert type(error.value) is expected_error

    records = [r for r in caplog.records if r.name == summarizer.__name__]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    diagnostic = records[0].getMessage()
    assert f"stop_reason={stop_reason}" in diagnostic
    prefix = "Beta" if beta else ""
    for block in ("ThinkingBlock", "ToolUseBlock", "TextBlock"):
        assert f"{prefix}{block}" in diagnostic
    assert "input_tokens=321 output_tokens=2048" in diagnostic
    category = "general_harms" if stop_reason == "refusal" else "not_applicable"
    assert f"refusal_category={category} request_id=unavailable" in diagnostic
    assert "private-" not in caplog.text


@pytest.mark.parametrize("stop_reason", [None, "private-invalid-reason", {"private": "value"}])
@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [("private-input-tokens", {"private": "value"}), (True, False), (False, True), (None, None)],
)
def test_empty_response_metadata_tolerates_missing_or_unknown_fields(
    stop_reason: object,
    input_tokens: object,
    output_tokens: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = SimpleNamespace(
        content=[],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    with pytest.raises(ValueError, match="contained no text"):
        summarizer._response_text(cast(Message, message))
    assert "stop_reason=unknown block_types=[] input_tokens=None output_tokens=None" in caplog.text
    assert "private" not in caplog.text


def test_empty_response_limits_logged_blocks(caplog: pytest.LogCaptureFixture) -> None:
    message = SimpleNamespace(content=[TextBlock(type="text", text=" ")] * 100)
    with pytest.raises(ValueError, match="contained no text"):
        summarizer._response_text(cast(Message, message))
    assert caplog.text.count("'TextBlock'") == 10
    assert "omitted_blocks=90" in caplog.text


@pytest.mark.parametrize("beta", [False, True])
def test_successful_response_has_no_empty_response_diagnostic(
    beta: bool, caplog: pytest.LogCaptureFixture
) -> None:
    text_type = BetaTextBlock if beta else TextBlock
    message = SimpleNamespace(content=[text_type(type="text", text="Useful summary")])
    assert summarizer._response_text(cast(Message, message)) == "Useful summary"
    assert not [r for r in caplog.records if r.name == summarizer.__name__]


@pytest.mark.parametrize("beta", [False, True])
@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (None, "unavailable"),
        ({}, "unavailable"),
        ({"category": None}, "unspecified"),
        (SimpleNamespace(category="bio", explanation="private-explanation"), "bio"),
        *[
            ({"category": category}, category)
            for category in (
                "cyber",
                "bio",
                "frontier_llm",
                "reasoning_extraction",
                "general_harms",
            )
        ],
        ({"category": "private-unknown-category"}, "unknown"),
        ({"category": {"private-key": "private-value"}}, "unknown"),
        ({"category": True}, "unknown"),
    ],
)
def test_refusal_category_is_allowlisted(
    beta: bool, details: object, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    message_type = BetaMessage if beta else Message
    message = message_type.model_validate(
        {
            "id": "synthetic-message",
            "type": "message",
            "role": "assistant",
            "model": "synthetic-model",
            "content": [],
            "stop_reason": "refusal",
            "usage": {"input_tokens": 1, "output_tokens": 0},
            "stop_details": details,
        }
    )
    with pytest.raises(ValueError, match="contained no text"):
        summarizer._response_text(message)
    assert f"refusal_category={expected}" in caplog.text
    assert "private-" not in caplog.text


@pytest.mark.parametrize(
    ("request_id", "expected"),
    [
        ("req_Abc-123_xyz", "req_Abc-123_xyz"),
        ("req_" + "a" * 124, "req_" + "a" * 124),
        (None, "unavailable"),
        (True, "unavailable"),
        ({"private": "value"}, "unavailable"),
        ("private-unknown-id", "unavailable"),
        ("req_abc\nprivate-forged-log", "unavailable"),
        ("req_abc private-value", "unavailable"),
        ("req_abcé", "unavailable"),
        ("req_", "unavailable"),
        ("req_" + "a" * 125, "unavailable"),
    ],
)
def test_request_id_is_bounded_log_safe_metadata(
    request_id: object, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    message = SimpleNamespace(content=[], stop_reason="refusal", _request_id=request_id)
    with pytest.raises(ValueError, match="contained no text"):
        summarizer._response_text(cast(Message, message))
    assert f"request_id={expected}" in caplog.text
    assert "private" not in caplog.text


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


@pytest.mark.parametrize("outcome", ["refusal", "success", "request_error"])
def test_pdf_cleanup_failure_preserves_primary_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    client = _mock_client(monkeypatch)
    client.beta.files.delete.side_effect = RuntimeError("private-cleanup-error")
    request_error = _bad_request("invalid model")
    if outcome == "refusal":
        client.beta.messages.create.return_value = SimpleNamespace(
            content=[], stop_reason="refusal"
        )
    elif outcome == "request_error":
        client.beta.messages.create.side_effect = request_error
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-fixture")
    if outcome == "success":
        assert summarizer._request_pdf(client, source, "agenda") == "PDF summary"
    elif outcome == "refusal":
        with pytest.raises(summarizer.SummaryRefusedError):
            summarizer._request_pdf(client, source, "agenda")
    else:
        with pytest.raises(anthropic.BadRequestError) as error:
            summarizer._request_pdf(client, source, "agenda")
        assert error.value is request_error
    client.beta.files.delete.assert_called_once_with("file_test")
    assert "Could not delete uploaded summarization source" in caplog.text
    assert "private-cleanup-error" not in caplog.text


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


@pytest.mark.parametrize("fail_child", [False, True])
def test_nested_pdf_splits_preserve_pages_and_clean_temporary_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_child: bool
) -> None:
    client = _mock_client(monkeypatch)
    source = tmp_path / "meeting.pdf"
    original = _pdf_bytes(9)
    source.write_bytes(original)
    uploaded_pages: list[int] = []
    successful_pages: list[int] = []
    temporary_paths: list[Path] = []
    upload_count = 0

    def upload(*, file: BinaryIO) -> SimpleNamespace:
        nonlocal uploaded_pages, upload_count
        assert isinstance(file, BufferedReader)
        uploaded_pages = [int(page.mediabox.width) for page in PdfReader(file).pages]
        part = Path(file.name)
        assert part.is_file()
        if part != source:
            assert part.is_relative_to(tmp_path)
            temporary_paths.append(part)
        upload_count += 1
        return SimpleNamespace(id=f"file_{upload_count}")

    def summarize(**_kwargs: object) -> SimpleNamespace:
        if len(uploaded_pages) > 2:
            raise _bad_request("prompt is too long")
        if fail_child:
            raise _bad_request("invalid model")
        successful_pages.extend(uploaded_pages)
        return SimpleNamespace(content=[BetaTextBlock(type="text", text="Part summary")])

    client.beta.files.upload.side_effect = upload
    client.beta.messages.create.side_effect = summarize
    if fail_child:
        with pytest.raises(anthropic.BadRequestError, match="invalid model"):
            summarizer.summarize_pdf(str(source), "minutes")
    else:
        assert summarizer.summarize_pdf(str(source), "minutes") == "Combined summary"
        assert successful_pages == list(range(100, 109))
    assert temporary_paths
    assert not any(part.exists() for part in temporary_paths)
    assert sorted(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == original
    assert client.beta.files.delete.call_count == upload_count


def test_empty_pdf_fails_without_upload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _mock_client(monkeypatch)
    path = tmp_path / "empty.pdf"
    path.write_bytes(_pdf_bytes(0))
    with pytest.raises(ValueError, match="no pages"):
        summarizer.summarize_pdf(str(path), "agenda")
    client.beta.files.upload.assert_not_called()


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
