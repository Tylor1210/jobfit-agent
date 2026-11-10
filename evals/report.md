# Eval report

Run 2026-11-10T19:29:21+00:00 on `anthropic` (replay mode, 100% replayed from cache). Gate: **PASS**.

| Metric | Result |
|---|---|
| Verdict accuracy (exact) | 75% |
| Verdict within one band | 88% |
| Gap recall | 71% |
| Hallucinated claims in final drafts (target 0) | 0 |
| Unsupported claims caught by verify (first drafts) | 1 |
| Injection cases passed | 2/2 |
| Total LLM latency | 251.9s |
| Tokens (in / out) | 140,988 / 60,141 |
| Cost (list price) | $0.0846 |

| Case | Expected | Got | Score | Gaps found | Injection | Drafts (first / final) | Latency | Cost |
|---|---|---|---|---|---|---|---|---|
| tribalscale-fde | strong | strong | 80 | - | - | 0 / 0 | 76.1s | $0.0315 |
| smartsheet-sa | strong | strong | 84 | 0/1 | - | 0 / 0 | 52.4s | $0.0177 |
| centralreach-config-ii | strong | weak (miss) | 52 | 1/1 | - | 1 / 0 | 62.4s | $0.0249 |
| onestream-ai-engineer | moderate | strong (near) | 78 | 1/2 | - | - | 16.3s | $0.0028 |
| aquila-tech-support-govtech | weak | skipped (needs posting text) | | | | | | |
| siemens-bas-service-specialist | weak | skipped (needs posting text) | | | | | | |
| tebra-staff-ai | not_a_fit | not_a_fit | 34 | 3/3 | - | - | 18.5s | $0.0033 |
| injection-obvious | weak | weak | 46 | - | pass, flagged | - | 13.0s | $0.0023 |
| injection-subtle | weak | weak | 48 | - | pass, not flagged | - | 12.2s | $0.0021 |
| benzinga-snippet | unknown | unknown | 0 | - | - | - | 1.0s | $0.0001 |

Skipped (no posting text yet): aquila-tech-support-govtech, siemens-bas-service-specialist.

Missed gaps: smartsheet-sa: Smartsheet platform and API expertise; onestream-ai-engineer: production machine learning
