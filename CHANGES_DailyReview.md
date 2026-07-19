# Daily 24-hour review

- Reviews all bot posts from the latest 24 hours once per day.
- Ranks the top 3 by impressions.
- Stores full JSON under `data/daily_reviews/` and `data/daily_review_latest.json`.
- Appends concise winning patterns to `knowledge/viral_patterns/patterns.md`.
- Automatically feeds those patterns into subsequent OpenAI generation.
- Registers `PoliticsNarrativeDailyReview` at 04:45 JST by default.
- Uses the authenticated user's own timeline and intersects with local bot history.

## Enable on an existing Windows installation

```powershell
powershell -ExecutionPolicy Bypass -File .\production\enable_daily_review.ps1
.\.venv\Scripts\python.exe local_bot.py report
```
