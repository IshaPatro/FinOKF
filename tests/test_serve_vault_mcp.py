import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "serve_vault.py"
SPEC = importlib.util.spec_from_file_location("serve_vault", MODULE_PATH)
serve_vault = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(serve_vault)


class YahooMCPBridgeTests(unittest.TestCase):
    def test_routes_market_questions_only(self):
        self.assertTrue(serve_vault.is_yahoo_market_question("What is AAPL's latest stock price?"))
        self.assertTrue(serve_vault.is_yahoo_market_question("How did it perform over one year?"))
        self.assertTrue(serve_vault.is_yahoo_market_question("Did Apple pay a dividend this year?"))
        self.assertTrue(serve_vault.is_yahoo_market_question("How much was Microsoft worth on November 1st 2025?"))
        self.assertTrue(serve_vault.is_yahoo_market_question("What was MSFT's market capitalization?"))
        self.assertFalse(serve_vault.is_yahoo_market_question("What was AAPL revenue in 2020?"))
        self.assertFalse(serve_vault.is_yahoo_market_question("Explain Apple's operating margin."))

    def test_market_data_agent_fetches_microsoft_and_hands_evidence_to_main_agent(self):
        question = "How much was Microsoft worth on November 1st 2025?"
        market_result = {
            "answer": "MSFT's Yahoo adjusted closing share price was 517.81 USD as of 2025-10-31.",
            "window": {"start": "2025-10-25", "end": "2025-11-01", "requested_date": "2025-11-01"},
            "interval": "1d",
            "data": [{"summary": {"ticker": "MSFT", "latest_adjusted_close": 517.81}}],
        }
        serve_vault.YAHOO_RESULT_CACHE.clear()
        serve_vault.YAHOO_NEXT_FETCH_AT = 0.0
        with patch.object(serve_vault, "call_yahoo_mcp", return_value=(market_result, 12.5)) as mcp_call:
            handoff, elapsed_ms = serve_vault.run_market_data_agent(question, "AAPL")

        self.assertTrue(handoff["decision"]["requires_yahoo"])
        self.assertEqual(handoff["decision"]["intent"], "valuation")
        self.assertEqual(handoff["ticker_hint"], "")
        self.assertEqual(handoff["result"], market_result)
        self.assertEqual(handoff["evidence_nodes"][0]["finokf"]["mcp_server"], "finokf-yahoo-finance")
        self.assertEqual(elapsed_ms, 12.5)
        mcp_call.assert_called_once_with(question, "")

        prompt_context = serve_vault.build_prompt_context({}, handoff["evidence_nodes"])
        self.assertIn("Structured Yahoo Finance MCP evidence for the main answer agent", prompt_context)
        self.assertIn('"latest_adjusted_close": 517.81', prompt_context)

    def test_market_data_agent_reuses_cached_yahoo_result(self):
        question = "How much was Microsoft worth on November 1st 2025?"
        market_result = {
            "answer": "MSFT was 517.81 USD.",
            "window": {"start": "2025-10-25", "end": "2025-11-01"},
            "interval": "1d",
            "data": [{"summary": {"ticker": "MSFT"}}],
        }
        serve_vault.YAHOO_RESULT_CACHE.clear()
        serve_vault.YAHOO_NEXT_FETCH_AT = 0.0
        with patch.object(serve_vault, "call_yahoo_mcp", return_value=(market_result, 10.0)) as mcp_call:
            first, first_ms = serve_vault.run_market_data_agent(question)
            second, second_ms = serve_vault.run_market_data_agent(question)

        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first_ms, 10.0)
        self.assertEqual(second_ms, 0.0)
        mcp_call.assert_called_once()

    def test_selected_ticker_is_only_needed_for_implicit_questions(self):
        self.assertFalse(serve_vault.question_has_explicit_security("How did it perform YTD?"))
        self.assertTrue(serve_vault.question_has_explicit_security("How did MSFT perform YTD?"))
        self.assertTrue(serve_vault.question_has_explicit_security("What is Microsoft's stock price?"))

    def test_calls_question_tool_over_mcp_stdio(self):
        tool_payload = {
            "answer": "AAPL is 123.45 USD.",
            "window": {"start": "2026-09-01", "end": "2026-09-04"},
            "interval": "1d",
        }
        responses = b"".join(
            [
                serve_vault.mcp_frame({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "finokf-yahoo-finance"}}}),
                serve_vault.mcp_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"content": [{"type": "text", "text": json.dumps(tool_payload)}]},
                    }
                ),
            ]
        )
        completed = SimpleNamespace(returncode=0, stdout=responses, stderr=b"")
        with patch.object(subprocess, "run", return_value=completed) as run:
            result, elapsed_ms = serve_vault.call_yahoo_mcp("What is its stock price?", "AAPL")

        self.assertEqual(result, tool_payload)
        self.assertGreaterEqual(elapsed_ms, 0)
        requests = serve_vault.parse_mcp_frames(run.call_args.kwargs["input"])
        self.assertEqual(requests[0]["method"], "initialize")
        self.assertEqual(requests[2]["method"], "tools/call")
        self.assertEqual(requests[2]["params"]["name"], "answer_yahoo_finance_question")
        self.assertEqual(requests[2]["params"]["arguments"]["ticker"], "AAPL")

    def test_yahoo_answer_includes_source_only_after_mcp_result(self):
        market_result = {
            "answer": "AAPL's latest available Yahoo price is 123.45 USD.",
            "window": {"start": "2026-09-01", "end": "2026-09-04"},
            "interval": "1d",
            "data": [{"summary": {"ticker": "AAPL", "regular_market_price": 123.45}}],
        }

        answer = serve_vault.answer_with_yahoo_source(market_result)
        self.assertIn("## Sources", answer)
        self.assertIn("[Yahoo Finance](https://finance.yahoo.com/quote/AAPL/)", answer)
        self.assertIn("`finokf-yahoo-finance` MCP", answer)
        self.assertIn("data through 2026-09-04", answer)

    def test_yahoo_evidence_node_renders_mcp_provenance_snapshot(self):
        market_result = {
            "answer": "AAPL moved 3.2%.",
            "window": {"start": "2026-01-01", "end": "2026-09-04"},
            "interval": "1d",
            "data": [{"summary": {"ticker": "AAPL", "percent_change_from_window_start": 3.2}}],
        }

        node = serve_vault.yahoo_evidence_node("How did AAPL perform YTD?", market_result)
        snapshot = serve_vault.build_snapshot_markdown("chat:test", node)
        self.assertEqual(node["type"], "finance.source")
        self.assertEqual(node["folder"], "sources")
        self.assertIn("# Yahoo Finance · AAPL", snapshot)
        self.assertIn("MCP server: `finokf-yahoo-finance`", snapshot)
        self.assertIn("https://finance.yahoo.com/quote/AAPL/", snapshot)
        self.assertIn('"percent_change_from_window_start": 3.2', snapshot)

    def test_yahoo_source_is_materialized_in_answer_vault_graph(self):
        market_result = {
            "answer": "AAPL's latest available Yahoo price is 123.45 USD.",
            "window": {"start": "2026-09-01", "end": "2026-09-04"},
            "interval": "1d",
            "data": [{"summary": {"ticker": "AAPL", "regular_market_price": 123.45}}],
        }
        source = serve_vault.yahoo_evidence_node("What is AAPL's latest price?", market_result)
        selected = {"id": "entity:aapl", "title": "Apple Inc.", "type": "finance.entity", "ticker": "AAPL", "path": "entities/AAPL.md"}
        execution = {
            "method": "auto",
            "route": "mcp-yahoo-finance",
            "cache_hit": False,
            "program": {"operation": "mcp_tool_call"},
            "bindings": [],
            "metrics": {},
        }
        skill = serve_vault.classify_skill("What is AAPL's latest price?")

        with tempfile.TemporaryDirectory(dir=serve_vault.ROOT) as temp_dir, patch.object(serve_vault, "VAULTS", Path(temp_dir)):
            vault = serve_vault.persist_chat_turn(
                None,
                "What is AAPL's latest price?",
                serve_vault.answer_with_yahoo_source(market_result),
                selected,
                [source],
                skill,
                execution,
            )
            source_nodes = [node for node in vault["nodes"] if node["type"] == "finance.source"]
            self.assertEqual(len(source_nodes), 1)
            source_path = serve_vault.ROOT / source_nodes[0]["path"]
            self.assertIn("Yahoo Finance", source_path.read_text(encoding="utf-8"))
            graph = json.loads((Path(temp_dir) / vault["slug"] / "graph.json").read_text(encoding="utf-8"))
            self.assertIn(source_nodes[0]["id"], [node["id"] for node in graph["nodes"]])

    def test_repeated_source_reuses_node_and_updates_cache_parameters(self):
        market_result = {
            "answer": "AAPL's latest available Yahoo price is 123.45 USD.",
            "window": {"start": "2026-09-01", "end": "2026-09-04"},
            "interval": "1d",
            "data": [{"summary": {"ticker": "AAPL", "regular_market_price": 123.45}}],
        }
        selected = {"id": "entity:aapl", "title": "Apple Inc.", "type": "finance.entity", "ticker": "AAPL", "path": "entities/AAPL.md"}
        skill = serve_vault.classify_skill("What is AAPL's latest price?")

        def execution():
            return {
                "method": "auto",
                "route": "mcp-yahoo-finance",
                "cache_hit": False,
                "program": {"operation": "mcp_tool_call"},
                "bindings": [],
                "metrics": {},
            }

        with tempfile.TemporaryDirectory(dir=serve_vault.ROOT) as temp_dir, patch.object(serve_vault, "VAULTS", Path(temp_dir)):
            first_source = serve_vault.yahoo_evidence_node("What is AAPL's latest price?", market_result)
            first = serve_vault.persist_chat_turn(None, "What is AAPL's latest price?", "First answer", selected, [first_source], skill, execution())
            first_node = first["nodes"][0]

            second_source = serve_vault.yahoo_evidence_node("Give me AAPL's latest price", market_result)
            second = serve_vault.persist_chat_turn(first["vault_id"], "Give me AAPL's latest price", "Second answer", selected, [second_source], skill, execution())

            self.assertEqual(len(second["nodes"]), 1)
            self.assertEqual(second["nodes"][0]["id"], first_node["id"])
            self.assertEqual(second["nodes"][0]["path"], first_node["path"])
            self.assertEqual(second["nodes"][0]["cache"]["status"], "hit")
            self.assertEqual(second["nodes"][0]["cache"]["use_count"], 2)
            self.assertEqual(second["metrics"]["evidence_cache_hits"], 1)
            self.assertEqual(second["metrics"]["evidence_cache_misses"], 0)
            graph = json.loads((Path(temp_dir) / second["slug"] / "graph.json").read_text(encoding="utf-8"))
            yahoo_nodes = [node for node in graph["nodes"] if node["type"] == "finance.source"]
            self.assertEqual(len(yahoo_nodes), 1)
            binds = [link for link in graph["links"] if link["rel"] == "binds"]
            self.assertEqual(len(binds), 2)
            self.assertEqual({link["target"] for link in binds}, {first_node["id"]})

    def test_legacy_duplicate_file_nodes_collapse_by_source_path(self):
        duplicate_nodes = [
            {
                "id": "snapshot:turn-001:entity:AAPL",
                "title": "Apple Inc.",
                "type": "finance.entity",
                "path": "data/vaults/example/notes/turn-001-AAPL.md",
                "source_id": "entity:AAPL",
                "source_path": "data/processed/companies/AAPL.md",
            },
            {
                "id": "snapshot:turn-002:entity:AAPL",
                "title": "Apple Inc.",
                "type": "finance.entity",
                "path": "data/vaults/example/notes/turn-002-AAPL.md",
                "source_id": "entity:AAPL",
                "source_path": "data/processed/companies/AAPL.md",
            },
        ]

        unique, by_identity = serve_vault.dedupe_cached_nodes(duplicate_nodes)
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(by_identity), 1)
        self.assertEqual(unique[0]["cache"]["identity"], "source:companies/AAPL.md")
        self.assertEqual(unique[0]["cache"]["use_count"], 2)


if __name__ == "__main__":
    unittest.main()
