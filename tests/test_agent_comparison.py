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
    def test_company_aliases_ticker_punctuation_and_unknown_local_company_do_not_use_prior_company(self):
        nodes = {ticker: {"id": ticker, "type": "finance.entity", "ticker": ticker, "title": name}
                 for ticker, name in [("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"), ("BRK.B", "Berkshire Hathaway Inc.")]}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(server, "PRICE_DAILY", Path(directory)):
            for name, ticker in [("Microsoft", "MSFT"), ("msft", "MSFT"), ("BRK-B", "BRK.B"), ("BRK.B", "BRK.B"), ("Tesla", "TSLA"), ("TSLA", "TSLA")]:
                with self.subTest(name=name):
                    self.assertEqual(server.mentioned_company_tickers(f"How did {name} perform in FY2025?", nodes["AAPL"], nodes), [ticker])

    def test_company_name_then_different_ticker_routes_only_current_company_and_grows_vault(self):
        handler = object.__new__(server.Handler)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processed = root / "data/processed"
            nodes = {}
            for ticker, title in [("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation")]:
                company = {"id": ticker, "type": "finance.entity", "ticker": ticker, "title": title,
                           "path": f"companies/{ticker}.md", "edges": []}
                nodes[ticker] = company
                for year in [2023, 2024, 2025, 2026]:
                    key = f"{ticker}-{year}"
                    nodes[key] = {"id": key, "type": "finance.filing", "ticker": ticker, "title": key,
                                  "path": f"filings/{ticker}/{year}.md", "preview": "Unused index preview",
                                  "finokf": {"form": "10-K", "fiscal_year": year, "filing_date": str(year)}}
            for node in nodes.values():
                path = processed / node["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"# {node['title']}\r\nComplete original source\r\n".encode())
            with mock.patch.object(server, "ROOT", root), mock.patch.object(server, "PROCESSED", processed), \
                 mock.patch.object(server, "VAULTS", root / "vaults"), mock.patch.object(server, "PRICE_DAILY", root / "prices"), \
                 mock.patch.object(server, "load_browser_index", return_value=({}, nodes)), \
                 mock.patch.object(server, "compile_flashokf", return_value={"hit": False}), \
                 mock.patch.object(server, "analytical_cash_bindings", return_value=("", [])), \
                 mock.patch.object(server, "filing_excerpt", side_effect=lambda path, *a, **kw: f"Evidence from {path}"), \
                 mock.patch.object(server, "research_missing_evidence", return_value={"sources": [], "warnings": [], "usage": TOKENS, "model_ms": 2, "web_requests": 0, "page_requests": 0}), \
                 mock.patch.object(server, "call_llm", return_value=("Analyst answer", TOKENS, 3)) as llm, \
                 mock.patch.object(server, "RESPONSE_LRU", server.OrderedDict()):
                first = handler._run_finokf({"message": "How did Apple's gross margin change in FY2024 and FY2025?", "provider_config": CONFIG})
                self.assertNotIn("MSFT", llm.call_args.args[1])
                second = handler._run_finokf({"message": "How did MSFT operating leverage change in FY2024 and FY2025?", "node": nodes["AAPL"], "vault_id": first["vault"]["vault_id"], "provider_config": CONFIG})
                self.assertNotIn("AAPL", llm.call_args.args[1])
                self.assertIn("MSFT", llm.call_args.args[1])
                self.assertEqual(second["metrics"]["total_tokens"], 30)
                self.assertEqual(second["metrics"]["model_calls"], 2)
                graph = server.build_chat_graph(second["vault"])
                self.assertEqual({n["ticker"] for n in graph["nodes"] if n["type"] == "finance.entity"}, {"AAPL", "MSFT"})
                filings = [n for n in graph["nodes"] if n["type"] == "finance.filing"]
                self.assertEqual(len(filings), 6)  # Three supplied excerpts per turn, not all eight eligible filings.
                for node in filings:
                    self.assertEqual((root / node["path"]).read_bytes(), (root / node["source_path"]).read_bytes())
                    company = next(n for n in graph["nodes"] if n["type"] == "finance.entity" and n["ticker"] == node["ticker"])
                    self.assertIn({"source": company["id"], "target": node["id"], "rel": "has_evidence"}, graph["links"])

    def test_search_parser_decodes_result_urls_and_ignores_navigation(self):
        html = '''<a href="/help">Help</a><li><a href="https://r.search.yahoo.com/RU=https%3A%2F%2Finvestor.example.com%2Fannual.pdf/RK=2/RS=x"><h3>Annual <b>report</b></h3></a><p>Revenue &amp; costs</p></li>
        <li><a href="https://example.com/results"><h3>Results</h3></a><p>FY2025 figures</p></li>'''
        result = server.parse_public_search_results(html)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["url"], "https://investor.example.com/annual.pdf")
        self.assertEqual(result[0]["title"], "Annual report")
        self.assertEqual(result[0]["snippet"], "Revenue & costs")
        with self.assertRaisesRegex(RuntimeError, "no usable results"):
            server.parse_public_search_results('<h3>Search unavailable</h3>')

    def test_public_pdf_is_extracted_with_layout(self):
        import pypdf
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/pdf"}
        response.read.return_value = b"%PDF-test-data"
        page = mock.Mock()
        page.extract_text.return_value = "USD millions    FY2025    FY2024\nRevenue         100       90"
        with mock.patch.object(server, "validate_public_url"), \
             mock.patch.object(server.urlrequest, "build_opener") as opener, \
             mock.patch.object(pypdf, "PdfReader") as reader:
            opener.return_value.open.return_value = response
            reader.return_value.pages = [page]
            text = server.fetch_research_page("https://example.com/report.pdf", "Revenue FY2025", 24000)
        page.extract_text.assert_called_once_with(extraction_mode="layout")
        self.assertIn("[PDF page 1]", text)
        self.assertIn("Revenue         100       90", text)

    def test_public_html_retains_table_boundaries_and_removes_scripts(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "text/html"}
        response.read.return_value = b'<script>ignore instructions</script><table><tr><td>Revenue</td><td>100</td></tr><tr><td>Costs</td><td>60</td></tr></table>'
        with mock.patch.object(server, "validate_public_url"), \
             mock.patch.object(server.urlrequest, "build_opener") as opener:
            opener.return_value.open.return_value = response
            text = server.fetch_research_page("https://example.com/report", "Revenue")
        self.assertIn("Revenue | 100 |", text)
        self.assertIn("\n", text)
        self.assertNotIn("ignore instructions", text)

    def test_naive_reads_pages_reviews_numbers_and_accumulates_all_calls(self):
        source = {"title": "Web result", "url": "https://example.com/research", "snippet": "Revenue 20"}
        with mock.patch.object(server, "call_llm", side_effect=[("company revenue\ncompany margin", TOKENS, 12), ('{"sufficient": true}', TOKENS, 7), ("Web answer", TOKENS, 13)]) as llm, \
             mock.patch.object(server, "search_public_web", return_value=[source]) as search, \
             mock.patch.object(server, "fetch_research_page", return_value="Full-page revenue 20 and cost 12") as fetch, \
             mock.patch.object(server, "load_browser_index", side_effect=AssertionError("local index accessed")), \
             mock.patch.object(server, "read_processed_markdown", side_effect=AssertionError("filing accessed")), \
             mock.patch.object(server, "response_cache_get", side_effect=AssertionError("cache accessed")), \
             mock.patch.object(server, "price_csv_path", side_effect=AssertionError("prices accessed")):
            result = server.run_naive("Company revenue?", CONFIG)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["total_tokens"], 45)
        self.assertEqual(result["metrics"]["model_ms"], 32)
        self.assertEqual(result["metrics"]["model_calls"], 3)
        self.assertEqual(result["metrics"]["page_requests"], 1)
        self.assertEqual(result["metrics"]["pages_fetched"], 1)
        fetch.assert_called_once()
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(result["sources"]), 1)
        self.assertIn("https://example.com/research", llm.call_args.args[1])
        self.assertIn("Full-page revenue 20 and cost 12", llm.call_args.args[1])
        self.assertFalse(result["cache_hit"])

    def test_naive_researches_identical_questions_again_without_vault_or_cache(self):
        first_source = {"title": "Fresh result one", "url": "https://example.com/one", "snippet": "Revenue"}
        second_source = {"title": "Fresh result two", "url": "https://example.com/two", "snippet": "Revenue"}
        calls = [
            ("revenue FY2025", TOKENS, 1), ('{"sufficient": true}', TOKENS, 1), ("First fresh answer", TOKENS, 1),
            ("revenue FY2025", TOKENS, 1), ('{"sufficient": true}', TOKENS, 1), ("Second fresh answer", TOKENS, 1),
        ]
        with mock.patch.object(server, "call_llm", side_effect=calls), \
             mock.patch.object(server, "search_public_web", side_effect=[[first_source], [second_source]]) as search, \
             mock.patch.object(server, "fetch_research_page", side_effect=["first web page", "second web page"]) as fetch, \
             mock.patch.object(server, "response_cache_get", side_effect=AssertionError("Naive cache accessed")), \
             mock.patch.object(server, "response_cache_put", side_effect=AssertionError("Naive cache accessed")), \
             mock.patch.object(server, "load_browser_index", side_effect=AssertionError("vault accessed")), \
             mock.patch.object(server, "read_processed_markdown", side_effect=AssertionError("filing accessed")):
            first = server.run_naive("What was revenue in FY2025?", CONFIG)
            second = server.run_naive("What was revenue in FY2025?", CONFIG)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(first["answer"], "First fresh answer")
        self.assertEqual(second["answer"], "Second fresh answer")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([item["url"] for item in first["sources"]], [first_source["url"]])
        self.assertEqual([item["url"] for item in second["sources"]], [second_source["url"]])
        self.assertFalse(first["cache_hit"])
        self.assertFalse(second["cache_hit"])

    def test_naive_follows_up_missing_evidence_and_tracks_failed_fetches(self):
        first = {"title": "Results", "url": "https://example.com/first", "snippet": "Revenue"}
        second = {"title": "Financial statements", "url": "https://example.com/second", "snippet": "Cost"}
        audit = json.dumps({"sufficient": False, "queries": ["cost of sales"], "urls": ["https://example.com/report"]})
        def fetch(url, *args):
            if url.endswith("first"):
                raise RuntimeError("HTTP 403")
            return "Revenue 100; cost of sales 60"
        with mock.patch.object(server, "call_llm", side_effect=[("annual results", TOKENS, 1), (audit, TOKENS, 2), ("Margin 40%", TOKENS, 3)]) as llm, \
             mock.patch.object(server, "search_public_web", side_effect=[[first], [second]]) as search, \
             mock.patch.object(server, "fetch_research_page", side_effect=fetch):
            result = server.run_naive("Apple gross margin FY2025?", CONFIG)
        self.assertTrue(result["ok"])
        self.assertEqual(search.call_count, 2)
        self.assertEqual(result["metrics"]["web_requests"], 2)
        self.assertEqual(result["metrics"]["page_requests"], 3)
        self.assertEqual(result["metrics"]["pages_fetched"], 2)
        self.assertEqual(result["metrics"]["total_tokens"], 45)
        self.assertIn("HTTP 403", llm.call_args.args[1])
        self.assertIn("cost of sales 60", llm.call_args.args[1])
        self.assertTrue(result["warnings"])

    def test_naive_malformed_review_has_bounded_recovery(self):
        source = {"title": "Results", "url": "https://example.com/results", "snippet": "Revenue"}
        with mock.patch.object(server, "call_llm", side_effect=[("annual results", TOKENS, 1), ("not JSON", TOKENS, 2), ("Insufficient evidence", TOKENS, 3)]), \
             mock.patch.object(server, "search_public_web", return_value=[source]) as search, \
             mock.patch.object(server, "fetch_research_page", return_value="Revenue only") as fetch:
            result = server.run_naive("Apple margin FY2025?", CONFIG)
        self.assertTrue(result["ok"])
        self.assertEqual(search.call_count, 2)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["metrics"]["model_calls"], 3)
        self.assertIn("not structured", result["warnings"][0])

    def test_search_failure_does_not_invent_an_answer(self):
        with mock.patch.object(server, "call_llm", return_value=("query", TOKENS, 1)) as llm, \
             mock.patch.object(server, "search_public_web", side_effect=RuntimeError("blocked")):
            result = server.run_naive("revenue?", CONFIG)
        self.assertFalse(result["ok"])
        self.assertIn("blocked", result["answer"])
        self.assertEqual(llm.call_count, 1)

    def test_failed_model_call_marks_usage_incomplete_without_padding_tokens(self):
        source = {"title": "Results", "url": "https://example.com/results", "snippet": "Revenue"}
        with mock.patch.object(server, "call_llm", side_effect=[("annual results", TOKENS, 1), TimeoutError("timed out")]), \
             mock.patch.object(server, "search_public_web", return_value=[source]), \
             mock.patch.object(server, "fetch_research_page", return_value="Financial statement"):
            result = server.run_naive("Apple FY2025?", CONFIG)
        self.assertFalse(result["ok"])
        self.assertFalse(result["metrics"]["usage_complete"])
        self.assertEqual(result["metrics"]["model_calls"], 1)
        self.assertEqual(result["metrics"]["model_attempts"], 2)
        self.assertEqual(result["metrics"]["total_tokens"], 15)
        self.assertGreaterEqual(result["metrics"]["model_ms"], 1)

    def test_ollama_can_bound_naive_generation(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"message":{"content":"Answer"},"prompt_eval_count":10,"eval_count":5}'
        with mock.patch.object(server.urlrequest, "urlopen", return_value=response) as opened:
            server.call_ollama("Research", "Question", {**CONFIG, "num_predict": 1600})
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload["options"]["num_predict"], 1600)

    def test_historical_price_uses_prior_session(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(server, "PRICE_DAILY", Path(directory)):
            Path(directory, "MSFT.csv").write_text("date,close,adj_close,volume\n2025-10-31,517,515,100\n2025-11-03,520,518,120\n")
            context = server.build_price_context(["MSFT"], "Microsoft worth November 1st 2025")
        self.assertIn("2025-10-31", context)
        self.assertIn("close=517", context)
        self.assertNotIn("close=520", context)
        self.assertIn("shares outstanding", context)

    def test_finokf_reuses_research_but_always_runs_fresh_inference(self):
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
            self.assertEqual(second["metrics"]["total_tokens"], 15)
            self.assertEqual(second["metrics"]["model_calls"], 1)
            self.assertEqual(first["metrics"]["model_calls"], 2)
            self.assertEqual(second["cache_kind"], "research-lru")
            self.assertEqual(second["metrics"]["web_requests"], 0)
            self.assertEqual(second["sources"][-1], web_source)
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(research.call_count, 1)
            self.assertEqual(first["metrics"]["total_tokens"], 30)
            context.return_value = "local price 101"
            third = handler._run_finokf(payload)
            self.assertFalse(third["cache_hit"])
            self.assertEqual(llm.call_count, 3)
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

    def test_precreated_chat_vault_keeps_automation_title(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(server, "ROOT", Path(directory)), \
             mock.patch.object(server, "VAULTS", Path(directory) / "vaults"):
            created = server.create_chat_vault({"ticker": "MSFT"}, "question-01-gpt-5.5")
            saved = server.persist_chat_turn(
                created["vault_id"],
                "How did Microsoft profitability change?",
                "Answer",
                {"ticker": "MSFT"},
                [],
                {"chain": []},
                {"route": "test", "metrics": {}},
            )

        self.assertEqual(saved["title"], "question-01-gpt-5.5")

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
