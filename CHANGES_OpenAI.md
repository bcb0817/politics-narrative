# OpenAI API migration changes

- Replaced the Anthropic SDK with the OpenAI Python SDK.
- Uses the Responses API with strict Structured Outputs.
- Default routing: `gpt-5-nano` for normal news and `gpt-5-mini` for important news.
- Limits important-model use to 8 successful calls per day by default.
- Tracks estimated token cost in `data/openai_usage.json`.
- Stops new LLM generation when the local monthly OpenAI estimate reaches USD 8 by default.
- Sets `PREFILTER_TOP_N=1` and `CANDIDATES_PER_NEWS=1` in the example configuration.
- Preserves text-only posting, conservative critique axes, quality scoring, duplicate checks, X posting, and Windows scheduled-task operation.

The cost file is an estimate based on configured rates. The OpenAI dashboard remains the billing source of truth.
