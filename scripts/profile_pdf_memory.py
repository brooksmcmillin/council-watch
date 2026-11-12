"""Offline PDF peak-RSS profile using the real SDK and a streaming fake transport.

Run each case in a fresh Linux process. No requests reach the network; no database,
scheduler or notifications are used. A size rejection exercises recursive splitting.
"""

import argparse
import hashlib
import json
import resource
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import anthropic
import httpx

from council_meetings import summarizer


class OfflineTransport(httpx.BaseTransport):
    def __init__(self, reject_above: int) -> None:
        self.reject_above = reject_above
        self.upload_sizes: dict[str, int] = {}
        self.deleted = 0
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if request.method == "POST" and request.url.path == "/v1/files":
            # Unlike MockTransport, do not materialize the entire multipart body.
            assert isinstance(request.stream, httpx.SyncByteStream)
            size = sum(len(chunk) for chunk in request.stream)
            file_id = f"file_{len(self.upload_sizes)}"
            self.upload_sizes[file_id] = size
            return httpx.Response(
                200,
                json={
                    "id": file_id,
                    "type": "file",
                    "filename": "meeting.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": size,
                    "created_at": "2026-11-12T00:00:00Z",
                    "downloadable": False,
                },
            )
        if request.method == "DELETE" and request.url.path.startswith("/v1/files/"):
            self.deleted += 1
            return httpx.Response(
                200, json={"id": request.url.path.rsplit("/", 1)[-1], "type": "file_deleted"}
            )
        if request.method == "POST" and request.url.path == "/v1/messages":
            try:
                body = json.loads(request.read())
            except json.JSONDecodeError as error:
                raise AssertionError("Invalid offline SDK message JSON") from error
            content = body["messages"][0]["content"]
            file_ids = [item["source"]["file_id"] for item in content if item["type"] == "document"]
            if self.reject_above and any(
                self.upload_sizes[key] > self.reject_above for key in file_ids
            ):
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "prompt is too long",
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "msg_offline",
                    "type": "message",
                    "role": "assistant",
                    "model": "offline-stub",
                    "content": [{"type": "text", "text": "Offline summary"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            )
        raise AssertionError("Unexpected offline request")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--reject-above-mib", type=int, default=0)
    parser.add_argument("--memory-limit-mib", type=int, default=1536)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS, (args.memory_limit_mib * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    with args.pdf.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    transport = OfflineTransport(args.reject_above_mib * 1024**2)
    client = anthropic.Anthropic(
        api_key="offline-not-a-secret",  # pragma: allowlist secret (offline transport only)
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )
    summarizer.anthropic.Anthropic = lambda **_: client  # type: ignore[assignment]
    start = time.monotonic()
    result: dict[str, Any] = {
        "pdf": args.pdf.name,
        "bytes": args.pdf.stat().st_size,
        "sha256": digest,
        "implementation_sha256": hashlib.sha256(Path(summarizer.__file__).read_bytes()).hexdigest(),
        "reject_above_mib": args.reject_above_mib,
        "memory_limit_mib": args.memory_limit_mib,
        "anthropic": version("anthropic"),
        "httpx": version("httpx"),
        "pypdf": version("pypdf"),
        "initial_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    try:
        summarizer.summarize_pdf(str(args.pdf), "agenda")
        result["status"] = "ok"
    except MemoryError:
        result["status"] = "memory_limit_exceeded"
    finally:
        client.close()
    result.update(
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        elapsed_seconds=round(time.monotonic() - start, 3),
        uploads=len(transport.upload_sizes),
        deleted=transport.deleted,
        requests=transport.requests,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
