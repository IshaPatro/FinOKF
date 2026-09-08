import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("graph_server", Path(__file__).parents[1] / "scripts" / "serve_vault.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class VaultGraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, value in (("ROOT", self.root), ("VAULTS", self.root / "vaults"), ("PROCESSED", self.root / "data/processed"), ("INDEX_PATH", self.root / "index.json")):
            patch = mock.patch.object(server, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "build_snapshot_markdown", side_effect=lambda vault, node: f"{vault}\n{node['preview']}")
        patch.start()
        self.addCleanup(patch.stop)
        self.apple = self.company("AAPL", "Apple")
        self.microsoft = self.company("MSFT", "Microsoft")
        for node in [self.apple, self.microsoft]:
            path = server.PROCESSED / node["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"---\r\nticker: {node['ticker']}\r\n---\r\n# {node['title']}\r\n\r\nOriginal complete note.  \r\n".encode())

    @staticmethod
    def company(ticker, title):
        return {"id": ticker, "title": title, "ticker": ticker, "type": "finance.entity",
                "path": f"companies/{ticker}.md", "preview": title}

    def persist(self, vault, selected, evidence=None, cache_hit=False):
        return server.persist_chat_turn(vault, f"Research {selected['title']}", "Answer", selected,
                                        evidence if evidence is not None else [selected], {"chain": []},
                                        {"route": "ollama-grounded-fallback", "cache_hit": cache_hit,
                                         "program": {"operation": "lru_cached_llm_fallback" if cache_hit else "llm_fallback"}})

    def graph(self, vault):
        return json.loads((self.root / vault["graph_path"]).read_text())

    def test_switching_company_shows_current_evidence_and_preserves_history(self):
        vault = self.persist(None, self.apple)
        vault = self.persist(vault["vault_id"], self.microsoft)
        graph = self.graph(vault)
        self.assertEqual({n.get("ticker") for n in graph["nodes"] if n.get("source_id")}, {"AAPL", "MSFT"})
        self.assertEqual(len(graph["nodes"]), 7)
        self.assertEqual(graph["nodes"][0]["title"], "Research Microsoft")
        answer_node = [node for node in graph["nodes"] if node.get("type") == "certifacts.answer"][-1]
        self.assertEqual(answer_node["question"], "Research Microsoft")
        self.assertEqual(answer_node["answer"], "Answer")
        self.assertEqual(len(vault["runs"]), 2)
        self.assertEqual(vault["runs"][0]["evidence_nodes"][0]["ticker"], "AAPL")
        self.assertEqual(vault["context"]["ticker"], "MSFT")
        for node in graph["nodes"]:
            self.assertTrue((self.root / node["path"]).is_file())

    def test_same_source_is_reused_and_usage_updated_on_cache_hit(self):
        vault = self.persist(None, self.microsoft, [self.microsoft, {**self.microsoft, "id": "alias"}])
        original = vault["nodes"][0]
        vault = self.persist(vault["vault_id"], self.microsoft, cache_hit=True)
        self.assertEqual(len(vault["nodes"]), 1)
        current = vault["nodes"][0]
        self.assertEqual(original["id"], current["id"])
        self.assertEqual(original["path"], current["path"])
        self.assertEqual(current["use_count"], 2)
        self.assertEqual(original["first_used_at"], current["first_used_at"])
        self.assertTrue(current["cache_hit"])
        graph = self.graph(vault)
        self.assertTrue(any("LRU cache hit" in n.get("title", "") for n in graph["nodes"]))
        self.assertTrue(any(link["rel"] == "reuses" for link in graph["links"]))

    def test_run_nodes_include_full_question_and_backfill_old_program_notes(self):
        vault = self.persist(None, self.microsoft)
        run = vault["runs"][0]
        program = self.root / run["paths"]["program"]
        self.assertIn("## Question\n\nResearch Microsoft", program.read_text())
        graph = self.graph(vault)
        self.assertEqual(graph["nodes"][1]["question"], "Research Microsoft")
        # Simulate the older program-only file, keeping its recorded execution.
        program.write_text("# Original cache program\n\nRecorded operation\n")
        server.backfill_chat_result_files()
        self.assertIn("Recorded operation", program.read_text())
        self.assertIn("## Question\n\nResearch Microsoft", program.read_text())
        saved = program.read_bytes()
        server.backfill_chat_result_files()
        self.assertEqual(program.read_bytes(), saved)

    def test_changed_source_preserves_old_snapshot_without_duplicate_node(self):
        vault = self.persist(None, self.microsoft)
        original = vault["nodes"][0]
        changed = {**self.microsoft, "preview": "Updated Microsoft evidence"}
        (server.PROCESSED / changed["path"]).write_text("# Updated full source\n")
        vault = self.persist(vault["vault_id"], changed)
        current = vault["nodes"][0]
        self.assertEqual(len(vault["nodes"]), 1)
        self.assertEqual(original["id"], current["id"])
        self.assertNotEqual(original["content_hash"], current["content_hash"])
        self.assertNotEqual(original["path"], current["path"])
        self.assertEqual(vault["runs"][0]["evidence_nodes"][0]["path"], original["path"])
        self.assertTrue((self.root / original["path"]).exists())

    def test_vaults_have_disjoint_graphs_even_when_reusing_same_evidence(self):
        first = self.persist(None, self.microsoft)
        first_graph_bytes = (self.root / first["graph_path"]).read_bytes()
        second = self.persist(None, self.microsoft, cache_hit=True)
        second = self.persist(second["vault_id"], self.apple)
        first_ids = {n["id"] for n in self.graph(first)["nodes"]}
        second_ids = {n["id"] for n in self.graph(second)["nodes"]}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual((self.root / first["graph_path"]).read_bytes(), first_graph_bytes)
        self.assertNotEqual(first["nodes"][0]["path"], second["nodes"][0]["path"])

    def test_comparisons_copy_each_actual_file_once_and_connect_company(self):
        path = server.PROCESSED / "filings/MSFT/report.md"
        path.parent.mkdir(parents=True)
        original = b"---\r\nticker: MSFT\r\n---\r\n# Full report\r\n\r\n| Revenue | 100 |\r\n| Income | 40 |\r\n"
        path.write_bytes(original)
        facts = [{**self.microsoft, "id": concept, "type": "finance.fact", "path": "filings/MSFT/report.md"} for concept in ("revenue", "income")]
        vault = self.persist(None, self.microsoft, [self.microsoft, self.apple, *facts])
        graph = self.graph(vault)
        self.assertEqual(len(graph["nodes"]), 6)
        self.assertEqual(len({n["id"] for n in graph["nodes"]}), 6)
        self.assertEqual({n.get("ticker") for n in graph["nodes"] if n.get("source_id")}, {"AAPL", "MSFT"})
        filing = next(n for n in graph["nodes"] if n.get("type") == "finance.filing")
        company = next(n for n in graph["nodes"] if n.get("type") == "finance.entity" and n["ticker"] == "MSFT")
        self.assertEqual((self.root / filing["path"]).read_bytes(), original)
        self.assertEqual(filing["title"], "report.md")
        self.assertIn({"source": company["id"], "target": filing["id"], "rel": "has_evidence"}, graph["links"])

    def test_legacy_yaml_citation_resolves_to_markdown_file_node_and_repairs_saved_label(self):
        name = "AAPL-FY2025-10-K-2025-10-31-0000320193-25-000079.md"
        canonical = f"filings/AAPL/{name}"
        old = "filings/AAPL/AAPL-FY2025-10-K-2025-09-27-0000320193-25-000079.yml"
        source = server.PROCESSED / canonical
        source.parent.mkdir(parents=True)
        source.write_bytes(b"# Complete filing\r\nOriginal financial tables\r\n")
        fact = {**self.apple, "id": "revenue-2023", "type": "finance.fact", "path": old,
                "title": "AAPL RevenueFromContractWithCustomerExcludingAssessedTax 2023-09-30"}
        evidence, aliases = server.canonical_evidence_paths([fact])
        self.assertEqual(aliases, {old: canonical})
        vault = self.persist(None, self.apple, evidence)
        file = next(n for n in vault["nodes"] if n["type"] == "finance.filing")
        self.assertEqual(file["title"], name)
        self.assertEqual(file["finokf"]["form"], "10-K")
        self.assertEqual(file["finokf"]["fiscal_year"], 2025)
        file["title"] = fact["title"]
        vault["runs"][0]["evidence_nodes"][0]["title"] = fact["title"]
        directory = self.root / "vaults" / vault["slug"]
        (directory / "index.json").write_text(json.dumps(vault))
        server.backfill_chat_result_files()
        updated = json.loads((directory / "index.json").read_text())
        corrected = next(n for n in updated["nodes"] if n["type"] == "finance.filing")
        self.assertEqual(corrected["title"], name)
        self.assertEqual((self.root / corrected["path"]).read_bytes(), source.read_bytes())
        self.assertEqual(server.source_files_for_run(updated["runs"][0])[0]["title"], name)

    def test_lru_reads_refresh_recency_and_eviction_removes_least_recent(self):
        with mock.patch.object(server, "RESPONSE_LRU", server.OrderedDict()), mock.patch.object(server, "RESPONSE_CACHE_SIZE", 2):
            server.response_cache_put("apple", {"answer": "A"})
            server.response_cache_put("microsoft", {"answer": "M"})
            server.response_cache_get("apple")["answer"] = "mutated copy"
            server.response_cache_put("amazon", {"answer": "Z"})
            self.assertIsNone(server.response_cache_get("microsoft"))
            self.assertEqual(server.response_cache_get("apple"), {"answer": "A"})
            self.assertEqual(server.response_cache_get("amazon"), {"answer": "Z"})

    def test_legacy_source_upgrade_retains_wrapper_and_original_metrics(self):
        vault = self.persist(None, self.microsoft)
        directory = self.root / "vaults" / vault["slug"]
        old = directory / "legacy.md"
        old.write_text("# Historical truncated wrapper\n")
        node = vault["runs"][0]["evidence_nodes"][0]
        node.update(path=server.vault_web_path(old), copy_mode="generated-evidence")
        vault["runs"][0]["metrics"] = {"total_tokens": 0}
        (directory / "index.json").write_text(json.dumps(vault))
        server.backfill_chat_result_files()
        updated = json.loads((directory / "index.json").read_text())
        copied = updated["runs"][0]["evidence_nodes"][0]
        self.assertEqual((self.root / copied["path"]).read_bytes(), (server.PROCESSED / self.microsoft["path"]).read_bytes())
        self.assertTrue(old.is_file())
        self.assertIn("copied_from_current_source_at", copied)
        self.assertEqual(updated["runs"][0]["metrics"], {"total_tokens": 0})


if __name__ == "__main__":
    unittest.main()
