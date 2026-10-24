from local_agent.config import WebAccessConfig
from local_agent.graph.state import ToolEvent
from local_agent.graph.web_budget import web_limit_reason


def test_search_budget_counts_failed_attempts_but_not_previous_runs() -> None:
    events = [ToolEvent(tool_call_id="a", tool_name="web_search", status="error", run_id="now"),
              ToolEvent(tool_call_id="b", tool_name="web_search", status="ok", run_id="old")]
    config = WebAccessConfig(max_search_calls=1)
    assert "预算" in web_limit_reason("web_search", {}, events=events, run_id="now", config=config)
    assert web_limit_reason("web_search", {}, events=events, run_id="next", config=config) is None


def test_page_and_continuation_limits_are_separate_and_ignore_fragments() -> None:
    events = [ToolEvent(tool_call_id="a", tool_name="web_fetch", status="ok", run_id="now",
                        redacted_arguments={"url": "https://example.com/a#one"})]
    config = WebAccessConfig(max_read_pages=1, max_reads_per_page=2)
    check = lambda url: web_limit_reason("web_fetch", {"url": url}, events=events, run_id="now", config=config)
    assert check("https://example.com/a#two") is None
    assert "页面预算" in check("https://example.com/b")
    events.append(ToolEvent(tool_call_id="b", tool_name="web_fetch", status="error", run_id="now",
                            redacted_arguments={"url": "https://example.com/a"}))
    assert "续读预算" in check("https://example.com/a")


def test_budget_does_not_raise_on_malformed_url_arguments() -> None:
    assert web_limit_reason("web_fetch", {"url": "http://["}, events=[], run_id="now", config=WebAccessConfig()) is None
