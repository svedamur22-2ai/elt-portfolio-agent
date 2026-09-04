import json

from src.utils.logging import log_event, wrap_node_with_logging


class TestLogEvent:
    def test_emits_one_json_line_with_required_fields(self, capsys):
        log_event("user_question", "req-1", question="What is the status?")
        line = capsys.readouterr().out.strip()
        payload = json.loads(line)
        assert payload["request_id"] == "req-1"
        assert payload["event"] == "user_question"
        assert "timestamp" in payload
        assert payload["question"] == "What is the status?"

    def test_redacts_a_top_level_credential_shaped_field(self, capsys):
        log_event("datasource_access", "req-1", api_token="sk-real-secret", source="Jira")
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["api_token"] == "***REDACTED***"
        assert payload["source"] == "Jira"

    def test_redacts_a_nested_credential_shaped_field(self, capsys):
        log_event("config_loaded", "req-1", config={"jira_url": "https://x.atlassian.net", "password": "hunter2"})
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["config"]["password"] == "***REDACTED***"
        assert payload["config"]["jira_url"] == "https://x.atlassian.net"

    def test_redacts_credential_shaped_keys_case_insensitively_and_by_substring(self, capsys):
        log_event("x", "req-1", OPENAI_API_KEY="sk-abc", MEM0_API_KEY="mk-abc", JIRA_API_TOKEN="tok-abc")
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["OPENAI_API_KEY"] == "***REDACTED***"
        assert payload["MEM0_API_KEY"] == "***REDACTED***"
        assert payload["JIRA_API_TOKEN"] == "***REDACTED***"

    def test_non_sensitive_fields_pass_through_unredacted(self, capsys):
        log_event("records_retrieved", "req-1", source="Finance", record_count=7)
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["record_count"] == 7
        assert payload["source"] == "Finance"


class TestWrapNodeWithLogging:
    def test_wraps_a_successful_node_with_start_and_end_events(self, capsys):
        def fake_node(state):
            return {"foo": "bar"}

        wrapped = wrap_node_with_logging("fake_node", fake_node)
        result = wrapped({"request_id": "req-1"})

        assert result == {"foo": "bar"}
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert [line["event"] for line in lines] == ["graph_node_start", "graph_node_end"]
        assert all(line["node"] == "fake_node" for line in lines)
        assert all(line["request_id"] == "req-1" for line in lines)
        assert isinstance(lines[1]["latency_ms"], (int, float))

    def test_a_raising_node_logs_error_then_still_raises(self, capsys):
        def broken_node(state):
            raise ConnectionError("simulated outage")

        wrapped = wrap_node_with_logging("broken_node", broken_node)

        try:
            wrapped({"request_id": "req-1"})
            raised = False
        except ConnectionError:
            raised = True

        assert raised
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert [line["event"] for line in lines] == ["graph_node_start", "error"]
        assert lines[1]["error_type"] == "ConnectionError"
        assert lines[1]["error_message"] == "simulated outage"

    def test_missing_request_id_falls_back_to_unknown_rather_than_crashing(self, capsys):
        wrapped = wrap_node_with_logging("fake_node", lambda state: {})
        wrapped({})
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert all(line["request_id"] == "UNKNOWN" for line in lines)
