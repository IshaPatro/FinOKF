# Turn 1 Naive Result

- Vault: `chat:msft-20260908t221140794429z-fdd1407f2e64`
- Question: How did the profitability of Microsoft's growth change across FY2023, FY2024 and FY2025? Assess consolidated operating leverage and whether the profitability of additional revenue strengthened or weakened.
- Provider: `openai`
- Model: `gpt-5.5`
- Route: `web-research`
- Cache hit: `false`

## Answer

On reported GAAP consolidated results, Microsoft’s average profitability improved each year, but the **profitability of incremental growth peaked in FY2024 and then weakened in FY2025**.

| Fiscal year | Revenue ($m) | Operating income ($m) | Operating margin | Δ Revenue YoY ($m) | Δ Op. income YoY ($m) | Incremental operating margin | Operating leverage* |
|---|---:|---:|---:|---:|---:|---:|---:|
| FY2023 | 211,915 | 88,523 | 41.8% | 13,645 | 5,140 | 37.7% | 0.90x |
| FY2024 | 245,122 | 109,433 | 44.6% | 33,207 | 20,910 | 63.0% | 1.51x |
| FY2025 | 281,724 | 128,528 | 45.6% | 36,602 | 19,095 | 52.2% | 1.17x |

\*Operating leverage = operating-income growth rate ÷ revenue growth rate.

**Assessment.** FY2023 showed weak/negative consolidated operating leverage: revenue rose 6.9%, while operating income rose only 6.2%, so operating income grew slightly slower than revenue. Incremental operating margin was 37.7%, below Microsoft’s 41.8% FY2023 operating margin.

FY2024 was the inflection: revenue grew 15.7%, operating income grew 23.6%, and incremental operating margin jumped to 63.0%. That means the FY2024 revenue growth was much more profitable than the existing revenue base, producing strong operating leverage.

FY2025 still had positive operating leverage—operating income grew 17.4% versus revenue growth of 14.9%—but the incremental margin fell to 52.2%. So the profitability of additional revenue **weakened versus FY2024**, even though it remained above Microsoft’s average FY2025 operating margin and therefore still expanded consolidated margin.

Key caveats: FY2023 reported operating income was affected by two disclosed items: a useful-life accounting estimate change increased FY2023 operating income by $3.7 billion, while a Q2 charge reduced it by $1.2 billion. Also, Microsoft says FY2025 segment recasts did **not** affect consolidated income statements. Microsoft’s FY2025 filing attributes gross-margin pressure partly to scaling AI infrastructure, consistent with lower incremental profitability in FY2025 despite continued margin expansion.

**Sources:** Microsoft FY2023 Annual Report: https://www.microsoft.com/investor/reports/ar23/index.html ; Microsoft FY2025 Annual Report: https://www.microsoft.com/investor/reports/ar25/index.html

## Sources

- 10-K - SEC.gov: https://www.sec.gov/Archives/edgar/data/789019/000095017023035122/msft-20230630.htm
- Images: https://images.search.yahoo.com/search/images;_ylt=AwrhQkMuiKBqSgIADtdXNyoA;_ylu=Y29sbwNiZjEEcG9zAzEEdnRpZAMEc2VjA3Nj?p=Microsoft+fy+2023+Form+10-K+annual+report+consolidated+statement+of+income+revenue+operating+income+SEC+investor+relations
- Form 10-K for Microsoft Corp filed 07/27/2023: https://microsoft.gcs-web.com/static-files/e2931fdb-9823-4130-b2a8-f6b8db0b15a9
- Microsoft Investor Relations - SEC Filings: https://www.microsoft.com/en-us/investor/sec-filings
- Microsoft 2023 Annual Report: https://www.microsoft.com/investor/reports/ar23/index.html
- EDGAR Filing Documents for 0000950170-23-035122 - SEC.gov: https://www.sec.gov/Archives/edgar/data/789019/000095017023035122/0000950170-23-035122-index.htm
- Microsoft – AI, Cloud, Productivity, Computing, Gaming & Apps: https://www.microsoft.com/en-us?msockid=2fefdf542b4168eb1453c89a2a1c69a0
- My Account: https://myaccount.microsoft.com/
- Microsoft account | Sign In or Create Your Account Today – Microsoft: https://account.microsoft.com/account
- Outlook: https://outlook.office.com/mail/
- Create your Microsoft account: https://signup.live.com/
- Home | Microsoft Careers: https://careers.microsoft.com/
- Images: https://images.search.yahoo.com/search/images;_ylt=AwrNOMNMiKBqUwIATvZXNyoA;_ylu=Y29sbwNiZjEEcG9zAzEEdnRpZAMEc2VjA3Nj?p=Microsoft+FY2025+Form+10-K+annual+report+consolidated+statement+of+income+revenue+operating+income+SEC+investor+relations+earnings+release
- Microsoft 2025 Annual Report: https://www.microsoft.com/investor/reports/ar25/index.html
- Form 10-K for Microsoft Corp filed 07/30/2025: https://microsoft.gcs-web.com/static-files/9640e86d-a99f-4502-934d-1d3c883fa786
- Microsoft Corp (MSFT) 10-K Annual Report July 2025: https://last10k.com/sec-filings/msft/0000950170-25-100235.htm
- SEC Filings - Microsoft Corporation: https://microsoft.gcs-web.com/financial-information/sec-filings

## Metadata

| Measure | Value |
| --- | ---: |
| prompt tokens | 34877 |
| completion tokens | 4846 |
| total tokens | 39723 |
| model ms | 74773.37 |
| total ms | 142266.324 |
| page requests | 6 |
| pages fetched | 3 |
| model calls | 3 |
| model attempts | 3 |
| usage complete | True |
| web requests | 3 |
| source count | 17 |
