# GPT-5.5 Retrospective Numerical Audit

Retrospective agent-assisted manual annotation; display-rounding tolerance; not a blinded human evaluation or exhaustive claim audit.

Each question has a fixed panel of explicitly reported margin levels, annual margin changes and incremental operating margins. Missing values are omissions, not incorrect claims. The panel is retrospective and does not exhaust acceptable ways to answer these questions.

| Question | Agent | Company | Metric | Year | Reference | Reported | Result |
|---|---|---|---|---|---|---|---|
| 1.1 | finokf | MSFT | om | 2023 | 41.772881 | 41.77 | correct |
| 1.1 | finokf | MSFT | om | 2024 | 44.644300 | 44.64 | correct |
| 1.1 | finokf | MSFT | om | 2025 | 45.621956 | 45.62 | correct |
| 1.1 | finokf | MSFT | iom | 2024 | 62.968651 | 62.97 | correct |
| 1.1 | finokf | MSFT | iom | 2025 | 52.169280 | 52.17 | correct |
| 1.1 | finokf | MSFT | om_bp | 2024 | 287.141894 | 287.1 | correct |
| 1.1 | finokf | MSFT | om_bp | 2025 | 97.765667 | 97.8 | correct |
| 1.1 | naive | MSFT | om | 2023 | 41.772881 | 41.8 | correct |
| 1.1 | naive | MSFT | om | 2024 | 44.644300 | 44.6 | correct |
| 1.1 | naive | MSFT | om | 2025 | 45.621956 | 45.6 | correct |
| 1.1 | naive | MSFT | iom | 2024 | 62.968651 | 63.0 | correct |
| 1.1 | naive | MSFT | iom | 2025 | 52.169280 | 52.2 | correct |
| 1.1 | naive | MSFT | om_bp | 2024 | 287.141894 | - | omitted |
| 1.1 | naive | MSFT | om_bp | 2025 | 97.765667 | - | omitted |
| 1.2 | finokf | MSFT | om | 2023 | 41.772881 | 41.77 | correct |
| 1.2 | finokf | MSFT | om | 2024 | 44.644300 | 44.64 | correct |
| 1.2 | finokf | MSFT | om | 2025 | 45.621956 | 45.62 | correct |
| 1.2 | finokf | MSFT | iom | 2024 | 62.968651 | 62.97 | correct |
| 1.2 | finokf | MSFT | iom | 2025 | 52.169280 | 52.17 | correct |
| 1.2 | finokf | MSFT | om_bp | 2024 | 287.141894 | 287.1 | correct |
| 1.2 | finokf | MSFT | om_bp | 2025 | 97.765667 | 97.8 | correct |
| 1.2 | naive | MSFT | om | 2023 | 41.772881 | - | omitted |
| 1.2 | naive | MSFT | om | 2024 | 44.644300 | - | omitted |
| 1.2 | naive | MSFT | om | 2025 | 45.621956 | 45.6 | correct |
| 1.2 | naive | MSFT | iom | 2024 | 62.968651 | - | omitted |
| 1.2 | naive | MSFT | iom | 2025 | 52.169280 | - | omitted |
| 1.2 | naive | MSFT | om_bp | 2024 | 287.141894 | - | omitted |
| 1.2 | naive | MSFT | om_bp | 2025 | 97.765667 | - | omitted |
| 1.3 | finokf | MSFT | om | 2024 | 44.644300 | 44.64 | correct |
| 1.3 | finokf | MSFT | om | 2025 | 45.621956 | 45.62 | correct |
| 1.3 | finokf | MSFT | iom | 2025 | 52.169280 | 52.17 | correct |
| 1.3 | finokf | MSFT | om_bp | 2025 | 97.765667 | 97.8 | correct |
| 1.3 | naive | MSFT | om | 2024 | 44.644300 | 44.6 | correct |
| 1.3 | naive | MSFT | om | 2025 | 45.621956 | 45.6 | correct |
| 1.3 | naive | MSFT | iom | 2025 | 52.169280 | 52.2 | correct |
| 1.3 | naive | MSFT | om_bp | 2025 | 97.765667 | 98 | correct |
| 2.1 | finokf | AAPL | gm | 2023 | 44.131130 | 44.13 | correct |
| 2.1 | finokf | AAPL | gm | 2024 | 46.206350 | 46.21 | correct |
| 2.1 | finokf | AAPL | gm | 2025 | 46.905164 | 46.91 | correct |
| 2.1 | finokf | AAPL | gm_bp | 2024 | 207.522024 | 207.5 | correct |
| 2.1 | finokf | AAPL | gm_bp | 2025 | 69.881429 | 69.9 | correct |
| 2.1 | naive | AAPL | gm | 2023 | 44.131130 | 44.13 | correct |
| 2.1 | naive | AAPL | gm | 2024 | 46.206350 | 46.21 | correct |
| 2.1 | naive | AAPL | gm | 2025 | 46.905164 | 46.91 | correct |
| 2.1 | naive | AAPL | gm_bp | 2024 | 207.522024 | 208.00 | correct |
| 2.1 | naive | AAPL | gm_bp | 2025 | 69.881429 | 70.00 | correct |
| 2.2 | finokf | AAPL | gm | 2023 | 44.131130 | 44.13 | correct |
| 2.2 | finokf | AAPL | gm | 2024 | 46.206350 | 46.21 | correct |
| 2.2 | finokf | AAPL | gm | 2025 | 46.905164 | 46.91 | correct |
| 2.2 | finokf | AAPL | gm_bp | 2024 | 207.522024 | 207.5 | correct |
| 2.2 | finokf | AAPL | gm_bp | 2025 | 69.881429 | 69.9 | correct |
| 2.2 | naive | AAPL | gm | 2023 | 44.131130 | - | omitted |
| 2.2 | naive | AAPL | gm | 2024 | 46.206350 | - | omitted |
| 2.2 | naive | AAPL | gm | 2025 | 46.905164 | - | omitted |
| 2.2 | naive | AAPL | gm_bp | 2024 | 207.522024 | - | omitted |
| 2.2 | naive | AAPL | gm_bp | 2025 | 69.881429 | - | omitted |
| 2.3 | finokf | AAPL | gm | 2024 | 46.206350 | 46.21 | correct |
| 2.3 | finokf | AAPL | gm | 2025 | 46.905164 | 46.91 | correct |
| 2.3 | finokf | AAPL | gm_bp | 2025 | 69.881429 | 69.9 | correct |
| 2.3 | naive | AAPL | gm | 2024 | 46.206350 | 46.2 | correct |
| 2.3 | naive | AAPL | gm | 2025 | 46.905164 | 46.9 | correct |
| 2.3 | naive | AAPL | gm_bp | 2025 | 69.881429 | 70 | correct |
| 2.4 | finokf | MSFT | om | 2025 | 45.621956 | 45.62 | correct |
| 2.4 | finokf | AAPL | om | 2025 | 31.970800 | 31.97 | correct |
| 2.4 | finokf | MSFT | om_bp | 2025 | 97.765667 | 97.8 | correct |
| 2.4 | finokf | AAPL | om_bp | 2025 | 46.057689 | 46.1 | correct |
| 2.4 | finokf | MSFT | iom | 2025 | 52.169280 | 52.17 | correct |
| 2.4 | finokf | AAPL | iom | 2025 | 39.138741 | 39.14 | correct |
| 2.4 | naive | MSFT | om | 2025 | 45.621956 | 45.6 | correct |
| 2.4 | naive | AAPL | om | 2025 | 31.970800 | 32.0 | correct |
| 2.4 | naive | MSFT | om_bp | 2025 | 97.765667 | 98 | correct |
| 2.4 | naive | AAPL | om_bp | 2025 | 46.057689 | 46 | correct |
| 2.4 | naive | MSFT | iom | 2025 | 52.169280 | - | omitted |
| 2.4 | naive | AAPL | iom | 2025 | 39.138741 | - | omitted |

Source paths, exact rows, SHA-256 hashes, tolerances and answer paths are recorded in audit.json.
