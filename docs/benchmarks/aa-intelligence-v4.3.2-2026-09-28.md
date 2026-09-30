# Artificial Analysis benchmarks: models used by Auto Office

Snapshot taken 2026-09-28 from artificialanalysis.ai (leaderboard and per-model pages).
Intelligence scores are **Artificial Analysis Intelligence Index v4.3.2** and are not
comparable with other index versions. See `catalog/seed.yaml` `benchmark_index_notes`.

Rows marked ★ (bold) were used by at least one dispatch in `runs.db` as of this date.

Columns:

- **Intel.**: AA Intelligence Index v4.3.2.
- **TB 4.0**: Terminal-Bench 4.0 (agentic coding and terminal use), percent.
- **SciCode**: SciCode, percent.
- **Coding**: mean of TB 4.0 and SciCode. Computed here, not an official AA Coding Index.
- **$ in / $ out**: list price per 1M input / output tokens.
- **$/task**: AA weighted cost per Intelligence Index task. This reflects AA's own harness
  and token usage, not ours.
- **Tok/s**: AA median output speed.
- **†**: deprecated on AA. No current cost per task or speed.

| # | Model (effort) | Intel. | TB 4.0 | SciCode | Coding | $ in /1M | $ out /1M | $/task | Tok/s |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Claude Opus 5.5 (max with fallback) | 57.6 | 59.6 | 66.9 | 63.2 | $4 | $20 | $5.98 | n/a |
| 2 | Claude Opus 5.5 (xhigh with fallback) | 56.0 | 59.6 | 65.0 | 62.3 | $4 | $20 | $3.46 | 89 |
| 3 | Claude Opus 5.5 (high with fallback) | 53.6 | 56.6 | 60.4 | 58.5 | $4 | $20 | $1.82 | 82 |
| 4 | GPT-6 Astra (max) | 52.7 | 59.1 | 56.5 | 57.8 | $10 | $50 | $3.26 | 63 |
| 5 | GPT-6 Astra (xhigh) | 52.4 | 59.6 | 55.7 | 57.6 | $10 | $50 | $2.31 | 56 |
| 6 | Claude Opus 5.5 (medium with fallback) | 51.2 | 52.5 | 59.3 | 55.9 | $4 | $20 | $1.34 | 81 |
| 7 | GPT-6 Astra (high) | 50.9 | 54.0 | 55.4 | 54.7 | $10 | $50 | $1.73 | 60 |
| 8 | Claude Opus 5 (max) † | 50.8 | 49.0 | 56.4 | 52.7 | $5 | $25 | n/a | n/a |
| 9 | Claude Opus 5 (xhigh) † | 49.7 | 46.5 | 55.7 | 51.1 | $5 | $25 | n/a | n/a |
| 10 | GPT-6 Astra (medium) | 49.6 | 49.5 | 54.2 | 51.8 | $10 | $50 | $1.54 | 55 |
| 11 | Claude Opus 5 (high) † | 48.1 | 46.0 | 55.4 | 50.7 | $5 | $25 | n/a | n/a |
| 12 | GPT-6 Sol (max) | 47.5 | 43.9 | 57.6 | 50.8 | $2 | $10 | $1.06 | 86 |
| **13** | **★ GPT-6 Astra (low)** | **45.8** | **41.9** | **54.1** | **48.0** | **$10** | **$50** | **$0.82** | **55** |
| **14** | **★ Claude Opus 5 (medium) †** | **44.8** | **34.3** | **51.5** | **42.9** | **$5** | **$25** | **n/a** | **n/a** |
| 15 | GPT-6 Sol (xhigh) | 44.1 | 30.3 | 55.1 | 42.7 | $2 | $10 | $0.53 | 83 |
| 16 | GPT-6 Sol (high) | 42.8 | 26.3 | 54.9 | 40.6 | $2 | $10 | $0.37 | 84 |
| **17** | **★ Claude Opus 5.5 (low with fallback)** | **42.3** | **31.3** | **58.6** | **44.9** | **$4** | **$20** | **$0.55** | **80** |
| 18 | Gemini 3.8 Flash (high) | 40.9 | 19.7 | 56.6 | 38.1 | $0.75 | $3.75 | $1.24 | 306 |
| 19 | GPT-6 Sol (medium) | 39.8 | 18.7 | 53.8 | 36.3 | $2 | $10 | $0.25 | n/a |
| **20** | **★ Gemini 3.8 Flash (medium)** | **39.8** | **19.7** | **55.1** | **37.4** | **$0.75** | **$3.75** | **$0.93** | **n/a** |
| 21 | Claude Opus 5 (low) † | 39.4 | 26.3 | 49.2 | 37.7 | $5 | $25 | n/a | n/a |
| 22 | Claude Sonnet 5 (max) | 38.2 | 14.1 | 54.3 | 34.2 | $2 | $10 | $5.09 | 84 |
| 23 | GPT-5.6 Luna (max) † | 37.3 | 11.6 | 53.6 | 32.6 | $0.2 | $1.2 | n/a | n/a |
| 24 | GPT-6 Luna (max) | 37.3 | 12.6 | 54.6 | 33.6 | $0.1 | $0.5 | $0.07 | 163 |
| **25** | **★ GPT-5.6 Luna (xhigh) †** | **34.6** | **3.5** | **50.5** | **27.0** | **$0.2** | **$1.2** | **n/a** | **n/a** |
| 26 | Claude Sonnet 5 (xhigh) | 34.4 | 7.1 | 54.1 | 30.6 | $2 | $10 | $2.87 | 70 |
| 27 | GPT-6 Sol (low) | 33.9 | 9.1 | 50.2 | 29.7 | $2 | $10 | $0.13 | 82 |
| **28** | **★ GPT-6 Luna (xhigh)** | **33.9** | **8.1** | **51.7** | **29.9** | **$0.1** | **$0.5** | **$0.04** | **152** |
| **29** | **★ Gemini 3.8 Flash (low)** | **33.5** | **10.1** | **55.0** | **32.5** | **$0.75** | **$3.75** | **n/a** | **n/a** |
| **30** | **★ GPT-6 Luna (high)** | **32.1** | **4.5** | **50.3** | **27.4** | **$0.1** | **$0.5** | **$0.03** | **146** |
| 31 | GPT-5.6 Luna (high) † | 32.1 | 2.5 | 51.6 | 27.1 | $0.2 | $1.2 | n/a | n/a |
| **32** | **★ Claude Sonnet 5 (high)** | **31.7** | **5.1** | **54.3** | **29.7** | **$2** | **$10** | **$1.79** | **66** |
| 33 | GPT-6 Luna (medium) | 29.5 | 2.5 | 50.9 | 26.7 | $0.1 | $0.5 | $0.02 | n/a |
| 34 | GPT-6 Sol (Non-reasoning) | 28.1 | 13.1 | 47.3 | 30.2 | $2 | $10 | $0.33 | 75 |
| 35 | Claude Sonnet 5 (medium) | 28.1 | 2.0 | 51.6 | 26.8 | $2 | $10 | $1.00 | 66 |
| 36 | GPT-5.6 Luna (medium) † | 25.0 | 0.5 | 46.8 | 23.6 | $0.2 | $1.2 | n/a | n/a |
| 37 | Claude Sonnet 5 (low) | 24.3 | 2.5 | 50.1 | 26.3 | $2 | $10 | $0.51 | 60 |
| 38 | Claude Sonnet 5 (Non-reasoning) | 23.2 | n/a | n/a | n/a | $2 | $10 | n/a | 66 |
| 39 | GPT-5.6 Luna (low) † | 21.0 | 0.0 | 46.1 | 23.0 | $0.2 | $1.2 | n/a | n/a |
| 40 | GPT-6 Luna (low) | 20.9 | 0.0 | 46.9 | 23.4 | $0.1 | $0.5 | $0.0045 | 142 |
| 41 | GPT-6 Luna (Non-reasoning) | 18.3 | 1.5 | 43.1 | 22.3 | $0.1 | $0.5 | $0.01 | 143 |
| 42 | GPT-5.6 Luna (Non-reasoning) † | 15.5 | 1.0 | 40.4 | 20.7 | $0.2 | $1.2 | n/a | n/a |

## Limits of this data

Benchmarks run each model in AA's harness, not ours. Observed behaviour in Auto Office
runs takes precedence when the two disagree:

- Claude Sonnet 5 (high) has been a reliable, cheap executor through the Claude Code
  harness despite its low Terminal-Bench score. Its per-token price is half of Opus 5.5.
- AA's $/task for Opus 5.5 (low) is lower than Sonnet 5 (high), but that depends on AA's
  token counts. Our own token usage has not confirmed it.
- GPT-6 Sol has used far more tokens in practice than its advertised cost suggests.
  It is not in any preferred seed.
