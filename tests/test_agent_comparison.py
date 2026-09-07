import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("comparison_server", Path(__file__).parents[1] / "scripts/serve_vault.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
CONFIG = {"provider": "ollama", "model": "test", "url": "http://127.0.0.1:11434", "api_key": "secret-test-key"}
TOKENS = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class ComparisonTests(unittest.TestCase):
    def test_naive_only_uses_web_and_accumulates_both_calls(self):
        source = {"title": "Web result", "url": "https://example.com/research", "snippet": "Revenue 20"}
        with mock.patch.object(server, "call_llm", side_effect=[("company revenue\ncompany margin", TOKENS, 12), ("Web answer", TOKENS, 13)]) as llm, \
             mock.patch.object(server, "search_public_web", return_value=[source]) as search, \
             mock.patch.object(server, "load_browser_index", side_effect=AssertionError("local index accessed")), \
             mock.patch.object(server, "read_processed_markdown", side_effect=AssertionError("filing accessed")), \
             mock.patch.object(server, "response_cache_get", side_effect=AssertionError("cache accessed")), \
             mock.patch.object(server, "price_csv_path", side_effect=AssertionError("prices accessed")):
            result = server.run_naive("Company revenue?", CONFIG)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["total_tokens"], 30)
        self.assertEqual(result["metrics"]["model_ms"], 25)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(result["sources"]), 1)
        self.assertIn("https://example.com/research", llm.call_args.args[1])
        self.assertFalse(result["cache_hit"])

    def test_search_failure_does_not_invent_an_answer(self):
        with mock.patch.object(server, "call_llm", return_value=("query", TOKENS, 1)) as llm, \
             mock.patch.object(server, "search_public_web", side_effect=RuntimeError("blocked")):
            result = server.run_naive("revenue?", CONFIG)
        self.assertFalse(result["ok"])
        self.assertIn("blocked", result["answer"])
        self.assertEqual(llm.call_count, 1)

    def test_historical_price_uses_prior_session(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(server, "PRICE_DAILY", Path(directory)):
            Path(directory, "MSFT.csv").write_text("date,close,adj_close,volume\n2025-10-31,517,515,100\n2025-11-03,520,518,120\n")
            context = server.build_price_context(["MSFT"], "Microsoft worth November 1st 2025")
        self.assertIn("2025-10-31", context)
        self.assertIn("close=517", context)
        self.assertNotIn("close=520", context)
        self.assertIn("shares outstanding", context)

    def test_finokf_cache_consumes_zero_tokens_and_changes_with_evidence(self):
        handler = object.__new__(server.Handler)
        selected = {"id": "MSFT", "ticker": "MSFT", "title": "Microsoft", "path": "", "type": "finance.entity"}
        payload = {"message": "Is MSFT a good buy?", "node": selected, "provider_config": CONFIG}
        web_source = {"title": "Yahoo Finance MSFT", "url": "https://finance.yahoo.com/quote/MSFT/", "snippet": "Dated quote",
                      "retrieved_at": "2026-09-07T12:00:00Z"}
        with mock.patch.object(server, "load_browser_index", return_value=({}, {"MSFT": selected})), \
             mock.patch.object(server, "gather_evidence", return_value=[]), \
             mock.patch.object(server, "add_retrieval_filings", return_value=[]), \
             mock.patch.object(server, "mentioned_company_tickers", return_value=["MSFT"]), \
             mock.patch.object(server, "build_prompt_context", return_value="local price 100") as context, \
             mock.patch.object(server, "persist_chat_turn", return_value={"vault_id": "test"}), \
             mock.patch.object(server, "find_chat_vault", return_value=None), \
             mock.patch.object(server, "research_missing_evidence", return_value={"sources": [web_source], "warnings": [], "usage": TOKENS,
                               "model_ms": 3, "web_requests": 1, "page_requests": 1}) as research, \
             mock.patch.object(server, "call_llm", return_value=("Local answer", TOKENS, 10)) as llm, \
             mock.patch.object(server, "RESPONSE_LRU", server.OrderedDict()):
            first = handler._run_finokf(payload)
            second = handler._run_finokf(payload)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["metrics"]["total_tokens"], 0)
            self.assertEqual(second["metrics"]["web_requests"], 0)
            self.assertEqual(second["sources"][-1], web_source)
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(research.call_count, 1)
            self.assertEqual(first["metrics"]["total_tokens"], 45)
            context.return_value = "local price 101"
            third = handler._run_finokf(payload)
            self.assertFalse(third["cache_hit"])
            self.assertEqual(llm.call_count, 4)
            self.assertEqual(third["metrics"]["web_requests"], 1)
            with mock.patch.object(server.time, "time", return_value=server.time.time() + 901):
                expired = handler._run_finokf(payload)
            self.assertFalse(expired["cache_hit"])
            self.assertEqual(research.call_count, 3)

    def test_web_fetch_rejects_private_network_targets(self):
        for url in ("file:///etc/passwd", "http://localhost:11434/api/tags", "http://user:pass@example.com/"):
            with self.assertRaises(ValueError):
                server.validate_public_url(url)
        with mock.patch.object(server.socket, "getaddrinfo", return_value=[(2, 1, 6, '', ('127.0.0.1', 80))]):
            with self.assertRaises(ValueError):
                server.validate_public_url("https://research.example.com")

    def test_web_evidence_is_inspectable_in_its_own_vault_graph(self):
        source = {"title": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/MSFT/",
                  "retrieved_at": "2026-09-07T12:00:00Z", "text": "Dated financial evidence"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(server, "ROOT", Path(directory)), \
             mock.patch.object(server, "VAULTS", Path(directory) / "vaults"):
            vault = server.persist_chat_turn(None, "Microsoft valuation", "Answer", {"ticker": "MSFT"},
                                            server.web_evidence_nodes([source]), {"chain": []}, {"route": "research"})
            graph = json.loads((Path(directory) / vault["graph_path"]).read_text())
            evidence = graph["nodes"][-1]
            self.assertEqual(evidence["source_path"], source["url"])
            snapshot = (Path(directory) / evidence["path"]).read_text()
            self.assertIn(source["url"], snapshot)
            self.assertIn(source["text"], snapshot)

    def test_analytical_cash_question_does_not_short_circuit_to_cash_balance(self):
        question = "Is Amazon's improvement in profitability translating into high-quality cash generation, or is the cash story weaker than the income statement suggests?"
        with mock.patch.object(server, "load_flashokf_facts", side_effect=AssertionError("scalar lookup attempted")):
            self.assertFalse(server.compile_flashokf(question, "AMZN")["hit"])

    def test_cash_conversion_only_uses_matching_periods(self):
        def fact(role, value, start, end):
            return {"id": role + end, "roles": [role], "value": value, "scale": "1000000", "unit": "USD",
                    "currency": "USD", "path": "filings/AMZN/report.md", "period": {"kind": "duration", "start": start, "end": end}}
        facts = [fact("net_income", "50", "2025-01-01", "2025-12-31"),
                 fact("operating_cash_flow", "100", "2025-01-01", "2025-12-31"),
                 fact("operating_cash_flow", "15", "2026-01-01", "2026-03-31")]
        with mock.patch.object(server, "load_flashokf_facts", return_value={"facts": facts}):
            text, nodes = server.analytical_cash_bindings("Cash conversion quality?", ["AMZN"])
        self.assertIn("2.000x", text)
        self.assertNotIn("2026-03-31", text)
        self.assertEqual(len(nodes), 2)

    def test_missing_evidence_is_fetched_and_full_page_text_supplied(self):
        source = {"title": "Amazon quarterly cash flows", "url": "https://ir.aboutamazon.com/results", "snippet": "Cash flow results"}
        with mock.patch.object(server, "call_llm", return_value=(json.dumps({"sufficient": False, "queries": ["AMZN cash flow capex earnings release"]}), TOKENS, 7)), \
             mock.patch.object(server, "search_public_web", return_value=[source]), \
             mock.patch.object(server, "fetch_research_page", return_value="Operating cash flow 100, capex 80; dated evidence."):
            result = server.research_missing_evidence("Cash quality?", ["AMZN"], "Cash balance only", CONFIG)
        self.assertEqual(result["web_requests"], 1)
        self.assertEqual(result["page_requests"], 1)
        self.assertIn("capex 80", result["sources"][0]["text"])
        self.assertEqual(server.web_evidence_nodes(result["sources"])[0]["path"], source["url"])

    def test_sufficient_evidence_does_not_search(self):
        with mock.patch.object(server, "call_llm", return_value=('{"sufficient": true, "queries": []}', TOKENS, 3)), \
             mock.patch.object(server, "search_public_web", side_effect=AssertionError("unnecessary search")):
            result = server.research_missing_evidence("CFO?", ["AMZN"], "CFO 100", CONFIG)
        self.assertEqual(result["web_requests"], 0)

    def test_stream_emits_finokf_before_naive_finishes(self):
        from threading import Event
        ready = Event()
        handler = object.__new__(server.Handler)
        payload = {"message": "Research Microsoft", "stream": True, "provider_config": CONFIG}
        events = []
        def slow_naive(*args):
            if not ready.wait(2):
                raise RuntimeError("FinOKF was not emitted while Naive was running")
            return {"ok": True, "agent": "naive", "answer": "Web", "metrics": TOKENS}
        def emit(event):
            events.append(event)
            if event["type"] == "answer" and event["answer"]["agent"] == "finokf":
                ready.set()
        with mock.patch.object(handler, "_read_json_body", return_value=payload), \
             mock.patch.object(handler, "send_response"), mock.patch.object(handler, "send_header"), \
             mock.patch.object(handler, "end_headers"), mock.patch.object(handler, "_stream_event", side_effect=emit), \
             mock.patch.object(handler, "_run_finokf", return_value={"ok": True, "agent": "finokf", "answer": "Local", "vault": {"vault_id": "test"}}), \
             mock.patch.object(server, "run_naive", side_effect=slow_naive), \
             mock.patch.object(server, "find_chat_vault", return_value=None):
            handler._handle_chat()
        self.assertEqual([e["answer"]["agent"] for e in events if e["type"] == "answer"], ["finokf", "naive"])
        self.assertTrue(events[-1]["answers"][0]["ok"])
        self.assertEqual(events[-1]["type"], "complete")

    def test_both_answers_and_metadata_persist_even_if_one_agent_fails(self):
        handler = object.__new__(server.Handler)
        payload = {"message": "Research Microsoft", "node": {"id": "MSFT", "ticker": "MSFT"}, "provider_config": CONFIG}
        naive = {"ok": True, "agent": "naive", "answer": "Web answer", "metrics": TOKENS}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(server, "ROOT", Path(directory)), \
             mock.patch.object(server, "VAULTS", Path(directory) / "vaults"), \
             mock.patch.object(handler, "_read_json_body", return_value=payload), \
             mock.patch.object(handler, "_run_finokf", side_effect=RuntimeError("model offline")), \
             mock.patch.object(server, "run_naive", return_value=naive), \
             mock.patch.object(handler, "_json") as response:
            handler._handle_chat()
            code, result = response.call_args.args
            self.assertEqual(code, 200)
            self.assertEqual(len(result["answers"]), 2)
            self.assertIsNone(result["persistence_error"])
            index = next((Path(directory) / "vaults").glob("*/index.json"))
            saved = json.loads(index.read_text())
            self.assertEqual([item.get("agent") for item in saved["messages"]], [None, "finokf", "naive"])
            self.assertEqual(saved["messages"][-1]["result"]["metrics"], TOKENS)
            self.assertNotIn(CONFIG["api_key"], index.read_text())
            self.assertIn("Naive", (index.parent / "chat.md").read_text())
            self.assertIn("Finokf", (index.parent / "chat.md").read_text())
            paths = saved["runs"][-1]["result_paths"]
            self.assertIn("results/turn-001-finokf.md", paths["finokf"])
            self.assertIn("results/turn-001-naive.md", paths["naive"])
            self.assertTrue((index.parent / "results" / "turn-001-finokf.json").is_file())
            self.assertTrue((index.parent / "results" / "turn-001-naive.json").is_file())
            self.assertIn("model offline", (index.parent / "results" / "turn-001-finokf.md").read_text())
            self.assertNotIn(CONFIG["api_key"], (index.parent / "results" / "turn-001-naive.json").read_text())

    def test_existing_chat_vaults_are_backfilled_with_result_files(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(server, "ROOT", Path(directory)), \
             mock.patch.object(server, "VAULTS", Path(directory) / "vaults"):
            server.VAULTS.mkdir(parents=True)
            index = server.create_chat_vault({"ticker": "MSFT", "id": "MSFT"}, "Old Microsoft chat")
            index["messages"] = [
                {"role": "user", "content": "Question", "turn": 1},
                {"role": "assistant", "agent": "finokf", "content": "Local answer", "turn": 1,
                 "result": {"agent": "finokf", "answer": "Local answer", "metrics": TOKENS, "api_key": "should-not-save"}},
                {"role": "assistant", "agent": "naive", "content": "Web answer", "turn": 1,
                 "result": {"agent": "naive", "answer": "Web answer", "metrics": TOKENS}},
            ]
            index["runs"] = [{"turn": 1, "question": "Question"}]
            index_path = server.VAULTS / index["slug"] / "index.json"
            index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

            server.backfill_chat_result_files()

            saved = json.loads(index_path.read_text())
            self.assertIn("result_paths", saved["runs"][-1])
            self.assertTrue((index_path.parent / "results" / "turn-001-finokf.md").is_file())
            self.assertTrue((index_path.parent / "results" / "turn-001-naive.json").is_file())
            self.assertNotIn("should-not-save", (index_path.parent / "results" / "turn-001-finokf.json").read_text())


if __name__ == "__main__":
    unittest.main()
