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
        for name, value in (("ROOT", self.root), ("VAULTS", self.root / "vaults")):
            patch = mock.patch.object(server, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(server, "build_snapshot_markdown", side_effect=lambda vault, node: f"{vault}\n{node['preview']}")
        patch.start()
        self.addCleanup(patch.stop)
        self.apple = self.company("AAPL", "Apple")
        self.microsoft = self.company("MSFT", "Microsoft")

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
        self.assertEqual({n.get("ticker") for n in graph["nodes"] if n.get("source_id")}, {"MSFT"})
        self.assertEqual(len(graph["nodes"]), 4)
        self.assertEqual(graph["nodes"][0]["title"], "Research Microsoft")
        answer_node = next(node for node in graph["nodes"] if node.get("type") == "certifacts.answer")
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
        self.assertIn("LRU cache hit", graph["nodes"][1]["title"])
        self.assertEqual(graph["links"][-1]["rel"], "reuses")

    def test_changed_source_preserves_old_snapshot_without_duplicate_node(self):
        vault = self.persist(None, self.microsoft)
        original = vault["nodes"][0]
        changed = {**self.microsoft, "preview": "Updated Microsoft evidence"}
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

    def test_comparisons_keep_both_companies_and_distinct_facts_from_same_filing(self):
        facts = [{**self.microsoft, "id": concept, "type": "finance.fact"} for concept in ("revenue", "income")]
        vault = self.persist(None, self.microsoft, [self.microsoft, self.apple, *facts])
        graph = self.graph(vault)
        self.assertEqual(len(graph["nodes"]), 7)
        self.assertEqual(len({n["id"] for n in graph["nodes"]}), 7)
        self.assertEqual({n.get("ticker") for n in graph["nodes"] if n.get("source_id")}, {"AAPL", "MSFT"})

    def test_lru_reads_refresh_recency_and_eviction_removes_least_recent(self):
        with mock.patch.object(server, "RESPONSE_LRU", server.OrderedDict()), mock.patch.object(server, "RESPONSE_CACHE_SIZE", 2):
            server.response_cache_put("apple", {"answer": "A"})
            server.response_cache_put("microsoft", {"answer": "M"})
            server.response_cache_get("apple")["answer"] = "mutated copy"
            server.response_cache_put("amazon", {"answer": "Z"})
            self.assertIsNone(server.response_cache_get("microsoft"))
            self.assertEqual(server.response_cache_get("apple"), {"answer": "A"})
            self.assertEqual(server.response_cache_get("amazon"), {"answer": "Z"})


if __name__ == "__main__":
    unittest.main()
