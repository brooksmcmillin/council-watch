from collections.abc import Iterator
from types import TracebackType

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from council_meetings.models import Base


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class FakeResponse:
    def __init__(self, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:  # pragma: no cover - never errors in tests
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        midpoint = len(self.content) // 2
        yield self.content[:midpoint]
        yield self.content[midpoint:]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeClient:
    """Minimal stand-in for httpx.Client recording HEAD/GET usage."""

    def __init__(
        self,
        *,
        get_content: bytes = b"pdf-bytes",
        head_length: int | None = None,
        head_error: bool = False,
        get_error: bool = False,
    ) -> None:
        self._get_content = get_content
        self._head_length = head_length
        self._head_error = head_error
        self._get_error = get_error
        self.get_calls = 0
        self.head_calls = 0

    def get(self, url: str) -> FakeResponse:
        self.get_calls += 1
        if self._get_error:
            raise httpx.HTTPError("boom")
        return FakeResponse(content=self._get_content)

    def stream(self, method: str, url: str) -> FakeResponse:
        assert method == "GET"
        return self.get(url)

    def head(self, url: str) -> FakeResponse:
        self.head_calls += 1
        if self._head_error:
            raise httpx.HTTPError("boom")
        headers = {}
        if self._head_length is not None:
            headers["content-length"] = str(self._head_length)
        return FakeResponse(headers=headers)
