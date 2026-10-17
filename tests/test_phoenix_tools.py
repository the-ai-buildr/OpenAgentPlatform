"""Phoenix trace summaries stay small enough to hand to an agent."""

from app.tools.phoenix_tools import list_phoenix_projects, summarize_span, summarize_trace


def test_span_summary_keeps_status_and_truncates_long_text():
    summary = summarize_span(
        {
            "id": "span-1",
            "name": "chat",
            "span_kind": "LLM",
            "status_code": "ERROR",
            "status_message": "boom",
            "start_time": "t0",
            "end_time": "t1",
            "context": {"trace_id": "abc"},
            "attributes": {
                "llm.model_name": "openai/gpt-6.1-sol-pro",
                "input.value": "x" * 500,
                "unused": "drop me",
            },
            "events": [{"name": "huge"}],
        }
    )

    assert summary["trace_id"] == "abc"
    assert summary["status_code"] == "ERROR"
    assert summary["attributes"]["llm.model_name"] == "openai/gpt-6.1-sol-pro"
    assert summary["attributes"]["input.value"].endswith("…")
    assert len(summary["attributes"]["input.value"]) == 401
    assert "unused" not in summary["attributes"]
    assert "events" not in summary


def test_trace_summary_is_the_identity_and_times():
    assert summarize_trace(
        {"id": "t", "trace_id": "hex", "start_time": "s", "end_time": "e", "spans": [1, 2]}
    ) == {"id": "t", "trace_id": "hex", "start_time": "s", "end_time": "e"}


def test_project_list_reports_a_connection_failure(monkeypatch):
    def explode(_path: str):
        raise TimeoutError("timed out")

    monkeypatch.setattr("app.tools.phoenix_tools._get_json", explode)
    monkeypatch.setattr(
        "app.tools.phoenix_tools.settings.otel_exporter_otlp_endpoint",
        "http://agentos-otel:6006",
    )

    result = list_phoenix_projects()

    assert result["status"] == "error"
    assert "timed out" in result["error"]
    assert result["base_url"] == "http://agentos-otel:6006"
