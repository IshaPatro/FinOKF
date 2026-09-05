import importlib.util
import io
import json
import sys
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "yahoo_finance_mcp.py"
SPEC = importlib.util.spec_from_file_location("yahoo_finance_mcp", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SAMPLE_CHART = {
    "chart": {
        "result": [
            {
                "meta": {
                    "currency": "USD",
                    "exchangeName": "NMS",
                    "instrumentType": "EQUITY",
                    "regularMarketPrice": 112.0,
                },
                "timestamp": [1704067200, 1704153600],
                "indicators": {
                    "quote": [
                        {
                            "open": [100.0, 110.0],
                            "high": [101.0, 113.0],
                            "low": [99.0, 109.0],
                            "close": [100.0, 112.0],
                            "volume": [1000, 2000],
                        }
                    ],
                    "adjclose": [{"adjclose": [100.0, 112.0]}],
                },
                "events": {"dividends": {"1704153600": {"amount": 0.24}}},
            }
        ],
        "error": None,
    }
}


class FakeYahooClient:
    def chart(self, ticker, start, end, interval):
        self.last_call = (ticker, start, end, interval)
        return SAMPLE_CHART


class YahooFinanceMCPTests(unittest.TestCase):
    def test_rows_from_chart_extracts_prices_and_events(self):
        meta, rows = MODULE.rows_from_chart(SAMPLE_CHART)
        self.assertEqual(meta["currency"], "USD")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["close"], 112.0)
        self.assertEqual(rows[1]["dividend"], 0.24)

    def test_answer_question_infers_ticker_and_return_intent(self):
        client = FakeYahooClient()
        result = MODULE.answer_question({"question": "How did Apple perform over one year?", "period": "1y"}, client)
        self.assertIn("AAPL moved 12 USD (12%)", result["answer"])
        self.assertEqual(result["data"][0]["summary"]["ticker"], "AAPL")
        self.assertEqual(client.last_call[3], "1d")

    def test_fetch_history_returns_tail_rows(self):
        result = MODULE.fetch_history(
            {"ticker": "AAPL", "start": "2024-01-01", "end": "2024-01-02", "max_rows": 1},
            FakeYahooClient(),
        )
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["window"]["start"], "2024-01-01")

    def test_mcp_initialize_and_tools_list(self):
        init = MODULE.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "finokf-yahoo-finance")
        listed = MODULE.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("answer_yahoo_finance_question", [tool["name"] for tool in listed["result"]["tools"]])

    def test_content_length_framing_round_trips(self):
        message = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
        raw = json.dumps(message).encode("utf-8")
        stream = io.BytesIO(b"Content-Length: " + str(len(raw)).encode("ascii") + b"\r\n\r\n" + raw)
        self.assertEqual(MODULE.read_message(stream), message)

        out = io.BytesIO()
        MODULE.write_message(out, {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertTrue(out.getvalue().startswith(b"Content-Length: "))

    def test_parse_price_window_accepts_explicit_dates(self):
        window = MODULE.parse_price_window({"start": "2024-01-01", "end": "2024-01-31"})
        self.assertEqual(window.start, date(2024, 1, 1))
        self.assertEqual(window.end, date(2024, 1, 31))

    def test_parse_price_window_understands_ordinal_date_and_weekend_fallback(self):
        window = MODULE.parse_price_window({}, "How much was Microsoft worth on November 1st 2025?")
        self.assertEqual(window.start, date(2025, 10, 25))
        self.assertEqual(window.end, date(2025, 11, 1))
        self.assertEqual(window.requested_date, date(2025, 11, 1))
        self.assertEqual(window.label, "on or before 2025-11-01")

    def test_worth_question_reports_historical_price_and_market_cap_limitation(self):
        client = FakeYahooClient()
        result = MODULE.answer_question(
            {"question": "How much was Microsoft worth on November 1st 2025?"},
            client,
        )

        self.assertEqual(client.last_call[:3], ("MSFT", date(2025, 10, 25), date(2025, 11, 1)))
        self.assertIn("closing share price", result["answer"])
        self.assertIn("historical shares outstanding", result["answer"])
        self.assertEqual(result["window"]["requested_date"], "2025-11-01")


if __name__ == "__main__":
    unittest.main()
