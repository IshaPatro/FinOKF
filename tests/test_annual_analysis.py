import importlib.util
from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("annual_server", Path(__file__).parents[1] / "scripts/serve_vault.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def fact(role, concept, year, value, scale="1000000"):
    return {"id": f"{role}:{year}", "roles": [role], "concept": f"us-gaap:{concept}",
            "value": value, "scale": scale, "unit": "USD", "currency": "USD",
            "path": "filings/TEST/annual.yml", "content_hash": "v1", "dimensions": {},
            "period": {"fiscal_year": year, "fiscal_period": "FY", "kind": "duration",
                       "start": f"{year}-01-01", "end": f"{year}-12-31"}}


class AnnualAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.facts = [fact("revenue", "Revenues", 2024, "100"),
                      fact("operating_income", "OperatingIncomeLoss", 2024, "10000000", "1"),
                      fact("revenue", "Revenues", 2025, "120"),
                      fact("operating_income", "OperatingIncomeLoss", 2025, "18")]
        self.question = "Compare TEST incremental operating margin for FY2024 and FY2025."
        for patch in (mock.patch.object(server, "RESPONSE_LRU", server.OrderedDict()),
                      mock.patch.object(server, "load_flashokf_facts", side_effect=lambda _: {"facts": self.facts})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_units_arithmetic_basis_points_and_semantic_lru(self):
        first = server.compile_flashokf(self.question, "TEST")
        self.assertTrue(first["hit"])
        self.assertFalse(first["lru_hit"])
        self.assertIn("40.00%", first["answer"])
        self.assertIn("+500.0 basis points", first["answer"])
        self.assertIn("10000000 × 1 USD", first["answer"])
        source_lines = [line for line in first["answer"].splitlines() if line.startswith("- `filings/TEST/annual.yml`")]
        self.assertEqual(source_lines, ["- `filings/TEST/annual.yml`"])
        self.assertIn("Measurements:", first["answer"])
        self.assertNotIn("Sources and measurements:", first["answer"])
        repeated = server.compile_flashokf("Calculate TEST operating leverage in FY2024 and FY2025.", "TEST")
        self.assertTrue(repeated["lru_hit"])
        self.assertEqual(first["answer"], repeated["answer"])
        self.facts[-1]["value"] = "20"
        updated = server.compile_flashokf(self.question, "TEST")
        self.assertFalse(updated["lru_hit"])
        self.assertIn("50.00%", updated["answer"])

    def test_historical_answer_source_section_collapses_duplicate_filing_paths(self):
        answer = "Conclusion.\n\nSources:\n- FY2024 revenue: `filings/TEST/annual.md`.\n- FY2024 cost: `filings/TEST/annual.md`.\n- `filings/TEST/other.md`\n"
        repaired = server.compact_answer_sources(answer)
        self.assertEqual(repaired.count("filings/TEST/annual.md"), 1)
        self.assertIn("filings/TEST/other.md", repaired)

    def test_missing_year_does_not_substitute_a_different_year(self):
        self.assertFalse(server.compile_flashokf("Compare TEST operating margin for FY2022 and FY2025.", "TEST")["hit"])
        self.assertFalse(server.compile_flashokf("What was TEST revenue in 2022?", "TEST")["hit"])

    def test_unsafe_measurements_fail_closed(self):
        for key, value in [("currency", "EUR"), ("unit", "shares"), ("value", "NaN"),
                           ("scale", "unknown"), ("dimensions", {"Segment": "Cloud"})]:
            with self.subTest(key=key):
                saved = deepcopy(self.facts[-1])
                self.facts[-1][key] = value
                self.assertFalse(server.compile_flashokf(self.question, "TEST")["hit"])
                self.facts[-1] = saved
        self.facts[-1]["period"]["start"] = "2025-04-01"
        self.assertFalse(server.compile_flashokf(self.question, "TEST")["hit"])

    def test_conflicting_consolidated_facts_require_reconciliation(self):
        self.facts.append({**self.facts[-1], "value": "99"})
        self.assertFalse(server.compile_flashokf(self.question, "TEST")["hit"])

    def test_compound_or_qualitative_request_is_not_answered_with_one_ratio(self):
        for question in ["Compare TEST operating margin and cash conversion in FY2025.",
                         "Compare TEST operating margin in FY2025 and explain the segment drivers.",
                         "Calculate TEST quarterly operating margin in FY2025."]:
            self.assertIsNone(server.compile_annual_analysis(question, "TEST"))

    def test_zero_or_negative_earnings_do_not_produce_misleading_cash_ratio(self):
        self.facts[:] = [fact("operating_cash_flow", "NetCashProvidedByUsedInOperatingActivities", 2025, "50"),
                         fact("net_income", "NetIncomeLoss", 2025, "-10")]
        for value in ["-10", "0"]:
            self.facts[-1]["value"] = value
            result = server.compile_flashokf("Calculate TEST cash conversion for FY2025.", "TEST")
            self.assertTrue(result["hit"])
            self.assertIn("Not meaningful", result["answer"])

    def test_handler_uses_cached_measurements_with_fresh_model_answers(self):
        handler = object.__new__(server.Handler)
        node = {"id": "TEST", "ticker": "TEST", "title": "Test company", "path": "", "type": "finance.entity"}
        payload = {"message": self.question, "node": node,
                   "provider_config": {"provider": "ollama", "model": "test", "url": "http://127.0.0.1:11434"}}
        with mock.patch.object(server, "load_browser_index", return_value=({}, {"TEST": node})), \
             mock.patch.object(server, "mentioned_company_tickers", return_value=["TEST"]), \
             mock.patch.object(server, "call_llm", side_effect=[("First interpretation", {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}, 0.01),
                                                               ("Fresh interpretation", {"prompt_tokens": 41, "completion_tokens": 21, "total_tokens": 62}, 0.01)]) as llm, \
             mock.patch.object(server, "research_missing_evidence", side_effect=AssertionError("web called")), \
             mock.patch.object(server, "build_prompt_context", side_effect=AssertionError("excerpts loaded")), \
             mock.patch.object(server, "persist_chat_turn", return_value={"vault_id": "test"}), \
             mock.patch.object(server, "find_chat_vault", return_value=None):
            first = handler._run_finokf(payload)
            second = handler._run_finokf(payload)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["cache_kind"], "measured-evidence")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["cache_kind"], "calculation-lru")
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(second["answer"], "Fresh interpretation")
        self.assertIn(self.question, llm.call_args.args[1])
        self.assertIn("40.00%", llm.call_args.args[1])
        self.assertEqual(first["metrics"]["total_tokens"], 60)
        self.assertEqual(second["metrics"]["total_tokens"], 62)
        for result in [first, second]:
            self.assertEqual(result["metrics"]["model_ms"], 0.01)
            self.assertEqual(result["metrics"]["model_calls"], 1)
            self.assertEqual(result["metrics"]["web_requests"], 0)

    def test_stress_mode_is_labeled_and_usage_is_sum_of_actual_calls(self):
        config = {"provider": "ollama", "model": "test"}
        tokens = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
        source = {"title": "Annual results", "url": "https://example.com/results", "snippet": "Results"}
        with mock.patch.object(server, "call_llm", side_effect=[("query", tokens, 2), ('{"sufficient": true}', tokens, 3), ("Draft", tokens, 4), ("Reviewed", tokens, 6)]), \
             mock.patch.object(server, "search_public_web", return_value=[source]), \
             mock.patch.object(server, "fetch_research_page", return_value="Annual results"):
            result = server.run_naive("Test annual margin?", config, True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["total_tokens"], 44)
        self.assertEqual(result["metrics"]["model_ms"], 15)
        self.assertEqual(result["experiment"], "verbose stress test")
        self.assertIn("stress test", result["answer"])

    def test_company_words_do_not_inject_unrelated_tickers_or_prior_company(self):
        nodes = {ticker: {"id": ticker, "ticker": ticker, "type": "finance.entity", "title": title}
                 for ticker, title in [("AAPL", "Apple Inc."), ("COST", "Costco Wholesale"), ("MSFT", "Microsoft") ]}
        self.assertEqual(server.mentioned_company_tickers(
            "Compare Apple's FY2024 and FY2025 cost of revenue.", nodes["MSFT"], nodes), ["AAPL"])
        self.assertEqual(server.mentioned_company_tickers(
            "Compare AAPL and COST FY2025 revenue.", {}, nodes), ["AAPL", "COST"])
