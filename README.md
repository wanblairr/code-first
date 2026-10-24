# Cafe Replenishment v3.7 · Final Source Package

Includes the date-region fallback and local ES Module compatibility fixes. The patch was deployed to the public website on **2026-10-24** and passed live page acceptance. Production deployment ID: `6adbafacd0d9a261aa1b1f98`. This package includes the local application, the corresponding public website source, the Apache 2.0 license, and the acceptance records for this release.

- [Live acceptance report](verification/public_acceptance_20261024/ACCEPTANCE_REPORT.txt)
- [Structured acceptance results](verification/public_acceptance_20261024/result.json)
- [Compatibility fix and original test records](verification/COMPATIBILITY_FIX.md)

The local Python workbench and public website use the same CSV recipes, safety-stock thresholds, previous-day sales weights, custom-purchase, and pricing logic. The Python workbench can run without deploying the website, and the core demo flow does not require any API keys.

- Public workbench: https://www.cafe-replenishment-demo.com/demo/
- Public sign-in / registration: https://www.cafe-replenishment-demo.com/demo/?account=login
- Default local address: http://127.0.0.1:8790/
- [First launch and configuration](START_GUIDE.md)
- [Complete demo steps and acceptance figures](DEMO_STEPS.md)

## Package Structure

| Path | Contents |
| --- | --- |
| START_LOCAL.cmd / 启动本机工作台.cmd | Windows local launchers; set a local access password on first run |
| Setup_Local_Password.cmd | Set a local access password only when no configuration exists; preserve existing configuration |
| local_app/ | Python source, web pages, original CSV files, bundled statistical model, tests, and dependency notes |
| public_website/ | Deployed public website source, including dist, Netlify functions, and Node tests |
| docs/ | Supplementary notes and demo documents |
| verification/ | Test and acceptance records for this delivery; actual recorded results are authoritative |

## Shared v3.7 Business Workflow

1. **Record yesterday’s sales**: Enter complete daily sales by product and size, following the CSV. Saving the same date again replaces the previous entry; it does not add to it. Recording historical sales does not deduct current inventory.
2. **Popularity**: Recent records are weighted with a 7-day half-life. Drinks and baked goods are evaluated separately. If fewer than 7 sales-record days are available, the app indicates that the sample is limited. Weighted average daily sales by product and size are combined with weather/holiday factors to estimate demand for the next day.
3. **Simulate a sale**: Deduct ingredients strictly according to the CSV BOM. Inventory below the CSV safety-stock threshold triggers a red warning. The most recent sale can be undone. Undo restores only the ingredients from that sale; it does not erase historical sales.
4. **Replenishment recommendations**: Ingredient shortfall = max(0, estimated usage for tomorrow + safety stock − remaining inventory). Package pricing is used to round up the number of packages to purchase and calculate the amount.
5. **Custom purchases**: Enter a name, whole-number quantity, unit, and MYR unit price. These items are included with BOM ingredients in the list, approval, and draft total. Prices for items with unknown costs are marked as pending confirmation, never shown as MYR 0.
6. **Manager approval**: Review and edit quantities, confirm any shortage warnings, then approve to generate TXT/EML drafts and a log. Approval does not add inventory or send email automatically.
7. **Optional integrations**: Gmail drafts, Runpod configuration/connection checks, the official WhatsApp API, and a personal sandbox entry point are retained. Local BOM recommendations use local sales weights and CSV formulas; this page does not provide a paid Runpod BOM review button, though the older review API/code remains. The public version still has request previews and a manual review flow. Each external action must be initiated separately.

These “weights” are interpretable statistical estimates with time decay; **they do not mean that the system has been trained on real store data**. When no history has been entered, the app continues to use forecasts clearly labeled as simulated. Weather can be switched to an explicitly simulated scenario; real weather can be refreshed manually. While the service is running, an automatic check is scheduled daily at 22:00 Kuala Lumpur time. If real weather is selected, the scheduled check may refresh the weather; explicitly simulated scenarios do not request real weather.

## Differences Between the Local and Public Versions

- The local version is a private workbench protected by a manager password. Business data is written to SQLite by the local Python service. This release does not add Google multi-account registration locally. Existing Gmail OAuth authorizes email drafts; it is not used for store sign-in.
- The public version uses Google sign-in, registers an account on first sign-in, isolates data by account, and stores store data in the cloud. Guest data is not automatically imported into a new account. As designed, WhatsApp sign-in is shown as “Coming soon”; the sending sandbox is not presented as a verification-code login method.
- Visitors provide their own API keys for the public version; local keys stay on the local machine. The final archive contains no personal API keys, OAuth credentials, store database, or signed-in session.
- The local Python interface keeps the current workbench style. The public version retains language options for English, Chinese, Bahasa Melayu, and Spanish.

The public source retains the deployed Llama reference panel; no Llama model is hosted publicly, and this package does not include its model weights or adapters. The reference panel does not imply that a free model service is available, and it does not replace replenishment calculations or manager approval.

## Data and Examples

The original files are `local_app/data/配料表.csv` and `local_app/data/安全线.csv`. The generated `local_app/data/bom-data.json` contains the BOM mapping read by the application. The public version retains the corresponding data-build script and generated assets. Safety-stock thresholds are determined by the supplied CSV; earlier illustrative examples such as “5 L of milk” are not current thresholds.

The delivery demo uses sample data only. Amounts in the demo instructions are based on explicitly entered prices and quantities; they are not supplier quotes. Dates are calculated using Asia/Kuala_Lumpur (UTC+8) on the day of launch; do not copy demo dates that have already passed.

This README, `START_GUIDE.md`, and `DEMO_STEPS.md` are the v3.7 entry documents. If older documentation is retained separately, use this package’s current documentation and the actual records in `verification/` for version numbers, test counts, ports, and integration status.

## Actual Acceptance for This Release on 2026-10-24

Yesterday’s sales were saved and remained available after refresh: 70 Oat Flat Whites and 35 Classic Lattes. Their popularity shares were 66.7% and 33.3%, respectively. Recording historical sales did not deduct current inventory. Simulating the sale of 40 medium Oat Flat Whites deducted 6,000 ml of oat milk and 720 g of house blend. Oat milk fell to 2,000 ml, below the 2,800 ml safety-stock threshold, and a red warning appeared. Undo restored each inventory item; oat milk returned to 8,000 ml and house blend to 12,200 g.

The simulated rainy-day recommendation showed a 3,500 ml shortfall. During acceptance, the purchase request was changed to 4,000 ml: 4 cartons of oat milk at 1 L each × MYR 12.00, plus 2 boxes of paper cups × MYR 10.25, for a total of **MYR 68.50**. Manager approval, the log, actual TXT/EML downloads, and data persistence after refresh were all accepted.

This release’s Gmail, Runpod, and official WhatsApp API/personal sandbox entry points were opened and checked. No new credentials were entered, no new Google authorization was initiated, no Gmail draft was created, no Runpod inference was submitted, and no WhatsApp message was sent. Successful real-integration records from earlier rounds do not mean those integrations were called again in this round. See the package acceptance report for detailed actions, order IDs, and scope.

## Verification and Costs

Python engineering tests: from `local_app`, run `python run_tests.py`. Local UI tests require Node.js 20+; `local_app/package.json` explicitly declares ES Module mode. Public version: Node.js 22.12+; enter `public_website` and run `npm ci`, `npm run test:backend`, and `npm run check:backend`. See `verification/` for full results. Platform-specific skips, network restrictions, and tests that were not run must be described separately; skipped tests must not be reported as passed.

The new local launchers do not call Runpod, Gmail, or WhatsApp, and they do not immediately request real weather. The retained daily 22:00 automatic check may refresh weather when real weather mode is selected. The core simulated demo has no external service fees. Manually calling the public Runpod integration, the older review API, or WhatsApp is subject to each platform’s quotas and pricing. This package does not automatically create cloud resources, change plans, or send supplier emails.

## License

Original project source code and documentation are licensed under the
[Apache License 2.0](LICENSE) (SPDX: `Apache-2.0`). See [NOTICE](NOTICE) for
attribution and the scope of this declaration. Third-party dependencies,
externally supplied datasets, model weights and adapters retain their own
applicable licenses and notices.
