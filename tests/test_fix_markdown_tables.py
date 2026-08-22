import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "fix_markdown_tables.py"
SPEC = importlib.util.spec_from_file_location("fix_markdown_tables", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MeasurementTests(unittest.TestCase):
    def repair(self, markdown: str) -> str:
        repaired, _ = MODULE.repair_document(markdown)
        return repaired

    def test_mixed_share_count_currency_and_per_share_scales(self):
        source = """Share activity follows (in millions, except number of shares, which are reflected in thousands, and per share amounts):

| Period | Total Number of Shares Purchased | Average Price Paid Per Share | Shares Purchased Under Plans |
| --- | --- | --- | --- |
| July | 41,690 | $ 145.91 | 41,690 |
| Total | 160,172 |  | $ 60,665 |
"""
        repaired = self.repair(source)
        self.assertIn("| July | 41,690K | $ 145.91 | 41,690K |", repaired)
        self.assertIn("| Total | 160,172K |  | $ 60,665M |", repaired)

    def test_row_scale_overrides_and_percentages(self):
        source = """| Metric | 2025 | 2024 | Change |
| --- | --- | --- | --- |
| Revenue (in millions) | $ 3,266 | $ 3,163 | 3 % |
| AUC/A at period end (in trillions) | $ 41.7 | $ 41.1 | 1 % |
| AUM at period end (in billions) | $ 2,214 | $ 2,211 | — % |
"""
        repaired = self.repair(source)
        self.assertIn("$ 3,266M", repaired)
        self.assertIn("$ 41.7T", repaired)
        self.assertIn("$ 2,214B", repaired)
        self.assertIn("| 1 % |", repaired)

    def test_column_scales_and_accounting_repair(self):
        source = """|  |  |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- | --- |
|  | Total Number of Shares |  | Average Price |  | Shares Under Plans | Maximum Shares |
| Period | (thousands) |  | Per Share |  | (thousands) | (millions) |
| Jan | 281 |  | $ | 480.30 | 281 | 18.4 |
"""
        repaired = self.repair(source)
        self.assertIn("| Jan | 281K | $480.30 | 281K | 18.4M |", repaired)

    def test_section_exception_and_share_section(self):
        source = """| In millions, except per share amounts | 2025 | 2024 |
| --- | --- | --- |
| Net income | $ 1,013 | $ 1,768 |
| Per Share Data |  |  |
| Basic | $ 0.81 | $ 1.41 |
| Average Shares Outstanding |  |  |
| Basic | 1,266 | 1,256 |
"""
        repaired = self.repair(source)
        self.assertIn("| Net income | $ 1,013M | $ 1,768M |", repaired)
        self.assertIn("| Basic | $ 0.81 | $ 1.41 |", repaired)
        self.assertIn("| Basic | 1,266M | 1,256M |", repaired)

    def test_period_headers_do_not_suppress_scale_and_share_counts_use_thousands(self):
        source = """(In millions, except number of shares which are reflected in thousands and per share amounts)

|  | Three Months Ended — June 27, 2020 | June 29, 2019 | June 27, 2020 | Nine Months Ended — June 29, 2019 |
| --- | --- | --- | --- | --- |
| Americas: |  |  |  |  |
| Net sales | $ 27,018 | $ 25,056 | $ 93,858 | $ 87,592 |
| Net income | $ 11,253 | $ 10,044 | $ 44,738 | $ 41,570 |
| Earnings per share: |  |  |  |  |
| Basic | $ 2.61 | $ 2.20 | $ 10.25 | $ 8.92 |
| Shares used in computing earnings per share: |  |  |  |  |
| Basic | 4,312,573 | 4,570,633 | 4,362,571 | 4,660,175 |
"""
        repaired = self.repair(source)
        self.assertIn(
            "| Net sales | $ 27,018M | $ 25,056M | $ 93,858M | $ 87,592M |",
            repaired,
        )
        self.assertIn(
            "| Net income | $ 11,253M | $ 10,044M | $ 44,738M | $ 41,570M |",
            repaired,
        )
        self.assertIn("| Basic | $ 2.61 | $ 2.20 | $ 10.25 | $ 8.92 |", repaired)
        self.assertIn(
            "| Basic | 4,312,573K | 4,570,633K | 4,362,571K | 4,660,175K |",
            repaired,
        )

    def test_exact_apple_four_period_operations_scenario(self):
        source = """(In millions, except number of shares which are reflected in thousands and per share amounts)

|  | Three Months Ended — June 25, 2022 | June 26, 2021 | Nine Months Ended — June 25, 2022 | June 26, 2021 |
| --- | --- | --- | --- | --- |
| Net sales: |  |  |  |  |
| Products | $ 63,355 | $ 63,948 | $ 245,241 | $ 232,309 |
| Services | 19,604 | 17,486 | 58,941 | 50,148 |
| Total net sales | 82,959 | 81,434 | 304,182 | 282,457 |
| Cost of sales: |  |  |  |  |
| Products | 41,485 | 40,899 | 155,084 | 149,476 |
| Services | 5,589 | 5,280 | 16,411 | 15,319 |
| Total cost of sales | 47,074 | 46,179 | 171,495 | 164,795 |
| Gross margin | 35,885 | 35,255 | 132,687 | 117,662 |
| Operating expenses: |  |  |  |  |
| Research and development | 6,797 | 5,717 | 19,490 | 16,142 |
| Selling, general and administrative | 6,012 | 5,412 | 18,654 | 16,357 |
| Total operating expenses | 12,809 | 11,129 | 38,144 | 32,499 |
| Operating income | 23,076 | 24,126 | 94,543 | 85,163 |
| Other income/(expense), net | ( 10 ) | 243 | ( 97 ) | 796 |
| Income before provision for income taxes | 23,066 | 24,369 | 94,446 | 85,959 |
| Provision for income taxes | 3,624 | 2,625 | 15,364 | 11,830 |
| Net income | $ 19,442 | $ 21,744 | $ 79,082 | $ 74,129 |
| Earnings per share: |  |  |  |  |
| Basic | $ 1.20 | $ 1.31 | $ 4.86 | $ 4.42 |
| Diluted | $ 1.20 | $ 1.30 | $ 4.82 | $ 4.38 |
| Shares used in computing earnings per share: |  |  |  |  |
| Basic | 16,162,945 | 16,629,371 | 16,277,824 | 16,772,656 |
| Diluted | 16,262,203 | 16,781,735 | 16,394,937 | 16,941,527 |
"""
        repaired = self.repair(source)
        self.assertIn(
            "| Products | $ 63,355M | $ 63,948M | $ 245,241M | $ 232,309M |",
            repaired,
        )
        self.assertIn(
            "| Services | $ 19,604M | $ 17,486M | $ 58,941M | $ 50,148M |",
            repaired,
        )
        self.assertIn(
            "| Other income/(expense), net | -$ 10M | $ 243M | -$ 97M | $ 796M |",
            repaired,
        )
        self.assertIn("| Basic | $ 1.20 | $ 1.31 | $ 4.86 | $ 4.42 |", repaired)
        self.assertIn(
            "| Basic | 16,162,945K | 16,629,371K | 16,277,824K | 16,772,656K |",
            repaired,
        )
        self.assertIn(
            "| Diluted | 16,262,203K | 16,781,735K | 16,394,937K | 16,941,527K |",
            repaired,
        )

    def test_000s_share_label_replaces_wrong_existing_scale(self):
        source = """**(amounts in millions) (unaudited)**

|  | 2025 | 2024 |
| --- | --- | --- |
| NET INCOME | $ 2,001M | $ 1,798M |
| NET INCOME PER COMMON SHARE: |  |  |
| Basic | $ 4.51 | $ 4.05 |
| Shares used in calculation (000s): |  |  |
| Basic | 443,837M | 443,377M |
| Diluted | 444,531M | 444,604M |
"""
        repaired = self.repair(source)
        self.assertIn("| Basic | 443,837K | 443,377K |", repaired)
        self.assertIn("| Diluted | 444,531K | 444,604K |", repaired)

    def test_securities_are_not_mistaken_for_share_counts(self):
        source = """(In millions, except number of shares which are reflected in thousands and par value)

| Assets | 2025 | 2024 |
| --- | --- | --- |
| Marketable securities | 20,729K | 27,699K |
"""
        repaired = self.repair(source)
        self.assertIn("| Marketable securities | $ 20,729M | $ 27,699M |", repaired)

    def test_citigroup_8k_mixed_millions_billions_and_change_columns(self):
        """Excerpt: C FY2022 8-K, filed 2022-05-10."""
        source = """| In millions of dollars, except as otherwise noted | 2021 | 2020 | % Change 2021 vs. 2020 |
| --- | --- | --- | --- |
| Net interest income | $ 20,646 | $ 22,326 | (8) % |
| Non-interest revenue | 2,681 | 2,814 | (5) |
| Income taxes | 2,207 | 334 | (76) |
| Balance Sheet data (in billions of dollars) |  |  |  |
| EOP assets | $ 464 | $ 453 | 2 % |
| Average assets | 467 | 454 | 3 |
| Efficiency ratio | 63 % | 54 % |  |
| Net credit losses as a percentage of average loans | 1.00 | 1.72 |  |
"""
        repaired = self.repair(source)
        self.assertIn("| Non-interest revenue | $ 2,681M | $ 2,814M | -5 |", repaired)
        self.assertIn("| Income taxes | $ 2,207M | $ 334M | -76 |", repaired)
        self.assertIn("| Average assets | $ 467B | $ 454B | 3 |", repaired)
        self.assertIn("| Efficiency ratio | 63 % | 54 % |  |", repaired)
        self.assertIn(
            "| Net credit losses as a percentage of average loans | 1.00 | 1.72 |  |",
            repaired,
        )

    def test_jpmorgan_10k_ranking_employee_and_ratio_exceptions(self):
        """Excerpt: JPM FY2024 10-K, filed 2025-02-14."""
        source = """| Selected metrics — As of or for the year ended December 31, (in millions, except ranking data, ratios and employees) | 2024 | 2023 | 2022 |
| --- | --- | --- | --- |
| 1 year ranking | 73 | 40 | 68 |
| Total assets | $ 255,385 | $ 245,512 | $ 232,037 |
| Loans | 236,303 | 227,929 | 214,006 |
| Employees | 29,403 | 28,485 | 26,041 |
| Net charge-offs/(recoveries) | $ 21 | $ 13 | $ (7) |
| Net charge-off/(recovery) rate | 0.01 % | 0.01 % | — % |
| Allowance for loan losses to period-end loans | 0.23 | 0.28 | 0.23 |
| Allowance for loan losses to nonaccrual loans | 77 | 97 | 108 |
| Nonaccrual loans to period-end loans | 0.30 | 0.29 | 0.21 |
"""
        repaired = self.repair(source)
        self.assertIn("| 1 year ranking | 73 | 40 | 68 |", repaired)
        self.assertIn("| Loans | $ 236,303M | $ 227,929M | $ 214,006M |", repaired)
        self.assertIn("| Employees | 29,403 | 28,485 | 26,041 |", repaired)
        self.assertIn("| Net charge-offs/(recoveries) | $ 21M | $ 13M | -$ 7M |", repaired)
        self.assertIn(
            "| Allowance for loan losses to period-end loans | 0.23 | 0.28 | 0.23 |",
            repaired,
        )
        self.assertIn(
            "| Allowance for loan losses to nonaccrual loans | 77 | 97 | 108 |",
            repaired,
        )

    def test_cvs_10q_membership_and_currency_column_inheritance(self):
        """Excerpts: CVS FY2026 10-Q, filed 2026-05-06."""
        source = """| In thousands | March 31, 2026 — Insured | ASC | Total |
| --- | --- | --- | --- |
| Medical membership: |  |  |  |
| Commercial | 2,462 | 15,872 | 18,334 |
| Medicare Advantage | 4,175 | — | 4,175 |

| In billions Authorization Date | Authorized | Remaining as of March 31, 2026 |
| --- | --- | --- |
| November 17, 2022 | $ 10.0 | $ 10.0 |
| December 9, 2021 | 10.0 | 1.5 |
"""
        repaired = self.repair(source)
        self.assertIn("| Commercial | 2,462K | 15,872K | 18,334K |", repaired)
        self.assertIn("| Medicare Advantage | 4,175K | — | 4,175K |", repaired)
        self.assertIn("| November 17, 2022 | $ 10.0B | $ 10.0B |", repaired)
        self.assertIn("| December 9, 2021 | $ 10.0B | $ 1.5B |", repaired)

    def test_starbucks_10k_mixed_share_per_share_life_and_value_units(self):
        """Excerpt: SBUX FY2022 10-K, filed 2022-11-18."""
        source = """RSU transactions (in millions, except per share and contractual life amounts):

|  | Number of Shares | Weighted Average Grant Date Fair Value per Share | Weighted Average Remaining Contractual Life (Years) | Aggregate Intrinsic Value |
| --- | --- | --- | --- | --- |
| Nonvested, October 3, 2021 | 7.7 | $ 86.23 | 0.9 | $ 869 |
| Granted | 4.2 | 107.71 |  |  |
| Vested | ( 3.7 ) | 80.02 |  |  |
| Nonvested, October 2, 2022 | 7.0 | 98.88 | 1.0 | 587 |
"""
        repaired = self.repair(source)
        self.assertIn("| Nonvested, October 3, 2021 | 7.7M | $ 86.23 | 0.9 | $ 869M |", repaired)
        self.assertIn("| Granted | 4.2M | 107.71 |  |  |", repaired)
        self.assertIn("| Vested | -3.7M | 80.02 |  |  |", repaired)
        self.assertIn("| Nonvested, October 2, 2022 | 7.0M | 98.88 | 1.0 | $ 587M |", repaired)

    def test_nike_def14a_column_scales_do_not_touch_percent_or_compensation(self):
        """Excerpts: NKE FY2025 DEF 14A, filed 2025-07-17."""
        source = """| FISCAL YEAR | STOCK OPTIONS AND SARS GRANTED (in millions) | FULL VALUE AWARDS GRANTED (in millions) | TOTAL GRANTED (in millions) | WEIGHTED-AVERAGE COMMON SHARES OUTSTANDING (in millions) | BURN RATE (%) |
| --- | --- | --- | --- | --- | --- |
| 2025 | 13.3 | 6.6 | 19.9 | 1,485 | 0.81 |

| YEAR | COMPENSATION ACTUALLY PAID | VALUE OF INITIAL FIXED $100 INVESTMENT | NET INCOME (IN MILLIONS) | ADJUSTED REVENUE (IN MILLIONS) |
| --- | --- | --- | --- | --- |
| 2025 | $ 17,010,238 | $ 65.09 | $ 3,201 | $ 46,350 |
"""
        repaired = self.repair(source)
        self.assertIn("| 2025 | 13.3M | 6.6M | 19.9M | 1,485M | 0.81 |", repaired)
        self.assertIn(
            "| 2025 | $ 17,010,238 | $ 65.09 | $ 3,201M | $ 46,350M |",
            repaired,
        )

    def test_jpmorgan_10q_financial_performance_multirow_header(self):
        """Excerpt: JPM FY2022 10-Q, filed 2022-08-03, lines 208-233."""
        source = """| Financial performance of JPMorgan Chase — (unaudited) As of or for the period ended, (in millions, except per share data and ratios) | Three months ended June 30, |  |  | Six months ended June 30, |  |  |
| --- | --- | --- | --- | --- | --- | --- |
|  | 2022 | 2021 | Change | 2022 | 2021 | Change |
| Selected income statement data |  |  |  |  |  |  |
| Noninterest revenue | $ 15,587 | $ 17,738 | (12) % | $ 32,432 | $ 37,115 | (13) % |
| Net interest income | 15,128 | 12,741 | 19 | 29,000 | 25,630 | 13 |
| Total net revenue | $ 30,715 | $ 30,479 | 1 | $ 61,432 | $ 62,745 | (2) |
| Total noninterest expense | 18,749 | 17,667 | 6 | 37,940 | 36,392 | 4 |
| Pre-provision profit | 11,966 | 12,812 | (7) | 23,492 | 26,353 | (11) |
| Provision for credit losses | 1,101 | (2,285) | NM | 2,564 | (6,441) | NM |
| Net income | 8,649 | 11,948 | (28) | 16,931 | 26,248 | (35) |
| Diluted earnings per share | $ 2.76 | $ 3.78 | (27) | $ 5.39 | $ 8.28 | (35) |
| Selected ratios and metrics |  |  |  |  |  |  |
| Return on common equity | 13% | 18% |  | 13% | 21% |  |
| Return on tangible common equity | 17 | 23 |  | 16 | 26 |  |
| Book value per share | $ 86.38 | $ 84.85 | 2 | $ 86.38 | $ 84.85 | 2 |
| Capital ratios (a) |  |  |  |  |  |  |
| CET1 capital | 12.2% | 13.0% |  | 12.2% | 13.0% |  |
| Tier 1 capital | 14.1 | 15.1 |  | 14.1 | 15.1 |  |
| Memo: |  |  |  |  |  |  |
| NII excluding Markets (b) | $ 13,682 | $ 10,863 | 26 | $ 25,434 | $ 21,638 | 18 |
| NIR excluding Markets (b) | 10,158 | 13,745 | (26) | 21,243 | 27,039 | (21) |
| Total net revenue - managed basis | $ 31,630 | $ 31,395 | 1 | $ 63,220 | $ 64,514 | (2) |
"""
        repaired = self.repair(source)
        self.assertIn(
            "| Noninterest revenue | $ 15,587M | $ 17,738M | -12 % | $ 32,432M | $ 37,115M | -13 % |",
            repaired,
        )
        self.assertIn(
            "| Net interest income | $ 15,128M | $ 12,741M | 19 | $ 29,000M | $ 25,630M | 13 |",
            repaired,
        )
        self.assertIn(
            "| Provision for credit losses | $ 1,101M | -$ 2,285M | NM | $ 2,564M | -$ 6,441M | NM |",
            repaired,
        )
        self.assertIn(
            "| Diluted earnings per share | $ 2.76 | $ 3.78 | -27 | $ 5.39 | $ 8.28 | -35 |",
            repaired,
        )
        self.assertIn("| Return on tangible common equity | 17 | 23 |  | 16 | 26 |  |", repaired)
        self.assertIn("| Tier 1 capital | 14.1 | 15.1 |  | 14.1 | 15.1 |  |", repaired)
        self.assertIn(
            "| NIR excluding Markets (b) | $ 10,158M | $ 13,745M | -26 | $ 21,243M | $ 27,039M | -21 |",
            repaired,
        )

    def test_jpmorgan_10q_revenue_scale_in_stub_header(self):
        """Excerpt: JPM FY2022 10-Q, filed 2022-08-03, lines 363-377."""
        source = """| Revenue |  |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- | --- |
|  | Three months ended June 30, |  |  | Six months ended June 30, |  |  |
| (in millions) | 2022 | 2021 | Change | 2022 | 2021 | Change |
| Investment banking fees | $ 1,586 | $ 3,470 | (54) % | $ 3,594 | $ 6,440 | (44) % |
| Principal transactions | 4,990 | 4,076 | 22 | 10,095 | 10,576 | (5) |
| Lending- and deposit-related fees | 1,873 | 1,760 | 6 | 3,712 | 3,447 | 8 |
| Asset management, administration and commissions | 5,240 | 5,194 | 1 | 10,602 | 10,223 | 4 |
| Investment securities losses | (153) | (155) | 1 | (547) | (141) | (288) |
| Mortgage fees and related income | 378 | 551 | (31) | 838 | 1,255 | (33) |
| Card income | 1,133 | 1,647 | (31) | 2,108 | 2,997 | (30) |
| Other income (a) | 540 | 1,195 | (55) | 2,030 | 2,318 | (12) |
| Noninterest revenue | 15,587 | 17,738 | (12) | 32,432 | 37,115 | (13) |
| Net interest income | 15,128 | 12,741 | 19 | 29,000 | 25,630 | 13 |
| Total net revenue | $ 30,715 | $ 30,479 | 1 % | $ 61,432 | $ 62,745 | (2) % |
"""
        repaired = self.repair(source)
        self.assertIn(
            "| Principal transactions | $ 4,990M | $ 4,076M | 22 | $ 10,095M | $ 10,576M | -5 |",
            repaired,
        )
        self.assertIn(
            "| Investment securities losses | -$ 153M | -$ 155M | 1 | -$ 547M | -$ 141M | -288 |",
            repaired,
        )
        self.assertIn(
            "| Total net revenue | $ 30,715M | $ 30,479M | 1 % | $ 61,432M | $ 62,745M | -2 % |",
            repaired,
        )

    def test_jpmorgan_2020_10q_split_accounting_parentheses_and_percent_column(self):
        """Malformed FY2020 JPM 10-Q values split `)` or `)%` into a spare column."""
        source = """| Financial performance of JPMorgan Chase — (unaudited) As of or for the period ended, (in millions, except per share data and ratios) | Three months ended March 31, |  |  |  |
| --- | --- | --- | --- | --- |
|  | 2020 | 2019 | Change |  |
| Selected income statement data |  |  |  |  |
| Total net revenue | $ 28,251 | $ 29,123 | (3 | )% |
| Total noninterest expense | 16,850 | 16,395 | 3 |  |
| Pre-provision profit | 11,401 | 12,728 | (10 | ) |
| Provision for credit losses | 8,285 | 1,495 | 454 |  |
| Net income | 2,865 | 9,179 | (69 | ) |
| Diluted earnings per share | $ 0.78 | $ 2.65 | (71 | ) |
| Selected ratios and metrics |  |  |  |  |
| Return on common equity | 4 % | 16 % |  |  |
| Return on tangible common equity | 5 | 19 |  |  |
| Book value per share | $ 75.88 | $ 71.78 | 6 |  |
| Tangible book value per share | 60.71 | 57.62 | 5 |  |
| Capital ratios (a) |  |  |  |  |
| CET1 | 11.5 % | 12.1 % |  |  |
| Tier 1 capital | 13.3 | 13.8 |  |  |
| Total capital | 15.5 | 15.7 |  |  |
"""
        repaired = self.repair(source)
        self.assertIn("| Total net revenue | $ 28,251M | $ 29,123M | -3 % |", repaired)
        self.assertIn("| Pre-provision profit | $ 11,401M | $ 12,728M | -10 |", repaired)
        self.assertIn("| Net income | $ 2,865M | $ 9,179M | -69 |", repaired)
        self.assertIn("| Diluted earnings per share | $ 0.78 | $ 2.65 | -71 |", repaired)
        self.assertIn("| Return on tangible common equity | 5 | 19 |  |", repaired)
        self.assertIn("| Tier 1 capital | 13.3 | 13.8 |  |", repaired)
        self.assertNotIn("| )", repaired)
        self.assertNotIn("| )%", repaired)

    def test_jpmorgan_2020_10q_standalone_percent_columns_are_removed(self):
        """Malformed FY2020 JPM 10-Q change percentages split `%` into spare columns."""
        source = """| Financial performance of JPMorgan Chase — (unaudited) As of or for the period ended, (in millions, except per share data and ratios) | Three months ended June 30, |  |  |  | Six months ended June 30, |  |  |  |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  | 2020 | 2019 | Change |  | 2020 | 2019 | Change |  |
| Selected income statement data |  |  |  |  |  |  |  |  |
| Total net revenue (a) | $ 32,980 | $ 28,747 | 15 | % | $ 61,172 | $ 57,823 | 6 | % |
| Total noninterest expense (a) | 16,942 | 16,256 | 4 |  | 33,733 | 32,604 | 3 |  |
| Pre-provision profit | 16,038 | 12,491 | 28 |  | 27,439 | 25,219 | 9 |  |
| Provision for credit losses | 10,473 | 1,149 | NM |  | 18,758 | 2,644 | NM |  |
| Net income | 4,687 | 9,652 | -51 |  | 7,552 | 18,831 | -60 |  |
| Diluted earnings per share | $ 1.38 | $ 2.82 | -51 |  | $ 2.17 | $ 5.49 | -60 |  |
"""
        repaired = self.repair(source)
        self.assertIn(
            "| Total net revenue (a) | $ 32,980M | $ 28,747M | 15 % | $ 61,172M | $ 57,823M | 6 % |",
            repaired,
        )
        self.assertIn(
            "| Total noninterest expense (a) | $ 16,942M | $ 16,256M | 4 | $ 33,733M | $ 32,604M | 3 |",
            repaired,
        )
        self.assertIn(
            "| Diluted earnings per share | $ 1.38 | $ 2.82 | -51 | $ 2.17 | $ 5.49 | -60 |",
            repaired,
        )
        self.assertNotIn("| % |", repaired)

    def test_no_declared_scale_is_unchanged_and_idempotent(self):
        source = """Before

| Year | Revenue |
| --- | --- |
| 2025 | $ 42 |

After
"""
        once = self.repair(source)
        twice = self.repair(once)
        self.assertEqual(source, once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
