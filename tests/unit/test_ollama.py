import threading

import pytest

from tango_posture.config import Config
from tango_posture.ollama import OllamaClient, base_url


def test_installed_models_and_streamed_analysis(ollama_server):
    url, server = ollama_server
    client = OllamaClient(
        Config(ollama_base_url=url, ollama_model="test-model:latest", ollama_timeout=2)
    )
    assert client.models() == ["test-model:latest"]
    chunks = []
    report = "# Report\nUNKNOWN at 00:00.2\n"
    text, complete = client.analyze(report, chunks.append, threading.Event())
    assert complete and text == "".join(chunks)
    payload = server.requests[0]
    assert payload["model"] == "test-model:latest" and payload["stream"] is True
    assert payload["options"]["num_ctx"] == 16384
    assert payload["messages"][-1]["content"].endswith(report)
    assert all("images" not in message for message in payload["messages"])


@pytest.mark.parametrize(
    "mode,error",
    [("missing", "model not found"), ("broken", "before completing"), ("empty", "empty analysis")],
)
def test_server_failures_are_actionable(ollama_server, mode, error):
    url, server = ollama_server
    server.mode = mode
    client = OllamaClient(Config(ollama_base_url=url, ollama_timeout=2))
    with pytest.raises((RuntimeError, ValueError), match=error):
        client.analyze("report", lambda _: None, threading.Event())


def test_cancel_and_generation_limit(ollama_server):
    url, server = ollama_server
    client = OllamaClient(Config(ollama_base_url=url, ollama_timeout=2))
    cancel = threading.Event()
    with pytest.raises(InterruptedError):
        client.analyze("report", lambda _: cancel.set(), cancel)
    server.mode = "limit"
    _, complete = client.analyze("report", lambda _: None, threading.Event())
    assert complete is False


def test_report_limit_does_not_silently_truncate_or_send(ollama_server):
    url, server = ollama_server
    client = OllamaClient(Config(ollama_base_url=url, ollama_max_report_chars=1000))
    with pytest.raises(ValueError, match="character limit"):
        client.analyze("x" * 1001, lambda _: None, threading.Event())
    assert server.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/report",
        "http://user:password@localhost:11434",
        "localhost:11434",
        "https://example.com/?secret=value",
    ],
)
def test_invalid_server_urls(url):
    with pytest.raises(ValueError):
        base_url(url)
