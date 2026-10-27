# GPT-6.1 Terra registration and 6.1 generation preference (2026-10-27)

User-requested new model registration, with 6.1 Sol and Terra executor preferred seeds before their 5.6 versions. This is a soft advisory preference: qualification, trust, user policy, cost and quotas still decide actual routing. No existing scores or trust settings changed. Older 5.6 versions remain valid fallback. Terra is discovery eligible but not automatically dispatchable until local exact model/effort conformance passes.

Benchmark: Artificial Analysis Intelligence Index v4.3.2. Fetched 2026-10-27T04:29:56Z. Model release: https://openai.com/index/introducing-gpt-6-1-terra/ . Model documentation: https://developers.openai.com/api/docs/models/gpt-6.1-terra . AA family: https://artificialanalysis.ai/models/releases/gpt-6-1-terra .

| Office effort | AA effort | Score | Exact AA page |
|---|---|---:|---|
| max | max | 48 | https://artificialanalysis.ai/models/gpt-6-1-terra |
| xhigh | xhigh | 45 | https://artificialanalysis.ai/models/gpt-6-1-terra-xhigh |
| high | high | 44 | https://artificialanalysis.ai/models/gpt-6-1-terra-high |
| medium | medium | 41 | https://artificialanalysis.ai/models/gpt-6-1-terra-medium |
| low | low | 36 | https://artificialanalysis.ai/models/gpt-6-1-terra-low |
| none | non-reasoning | 33 | https://artificialanalysis.ai/models/gpt-6-1-terra-non-reasoning |

Known pricing: USD 1 input and USD 5 output per million tokens. Unknown speed is null. Benchmark-only evidence does not verify Codex invocations. Non-reasoning maps to Office none but still requires conformance.

Validation: sandbox YAML parse and six-entry check passed (before timeout); full tests were not executed. Keep PR draft until CI and an independent review pass.
