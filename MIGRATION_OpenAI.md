# OpenAI API migration

This version replaces the Anthropic SDK with the OpenAI Python SDK and the Responses API.

## Defaults

- Normal news: `gpt-5-nano`
- Important news: `gpt-5-mini`
- Important-model limit: 8 calls/day
- One news item and one generated candidate per post slot
- Monthly OpenAI estimated budget: USD 8
- Structured Outputs preserves the existing scoring and text-diagram fields

## Windows upgrade

```powershell
cd "D:\SNS Bot\politics-narrative"
powershell -ExecutionPolicy Bypass -File .\production\stop.ps1
powershell -ExecutionPolicy Bypass -File .\production\migrate_to_openai.ps1
```

Enter `OPENAI_API_KEY` in `.env`, save it, then run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe local_bot.py force
powershell -ExecutionPolicy Bypass -File .\production\start.ps1
```

The local estimate is stored in `data/openai_usage.json`. It is a guardrail, not an invoice; the OpenAI dashboard remains the source of truth.
