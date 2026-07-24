"""Local-only weekly Shorts and note draft production.

This module writes draft files only. It has no publishing, browser automation,
note API, YouTube API, or X write capability.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import forecast
from metrics_db import apply_additive_migrations, connect, db_path, write


JST = ZoneInfo("Asia/Tokyo")


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _week_start(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    today = datetime.now(JST).date()
    return today - timedelta(days=today.weekday())


def _weekly_source(week: date) -> Path | None:
    directory = _root() / "reports" / "weekly"
    if not directory.exists():
        return None
    candidates = sorted(directory.glob("*.md"), reverse=True)
    exact = directory / f"{week.isoformat()}.md"
    return exact if exact.exists() else (candidates[0] if candidates else None)


def _safe_themes(week: date, path: Path) -> list[dict]:
    end = week + timedelta(days=7)
    with closing(connect(path)) as conn:
        return [dict(row) for row in conn.execute("""SELECT p.tweet_id,p.topic_key,
          p.text,p.post_type,p.critique_axis,p.posted_at,
          COALESCE(m.impressions,0) impressions,
          q.anger_score,q.trust_score,q.correction_required,
          q.manual_delete_required
          FROM published_posts p
          LEFT JOIN post_metrics m ON m.id=(SELECT id FROM post_metrics
            WHERE tweet_id=p.tweet_id ORDER BY
            CASE measurement_window WHEN '72h' THEN 3 WHEN '24h' THEN 2
            WHEN '1h' THEN 1 ELSE 0 END DESC,id DESC LIMIT 1)
          LEFT JOIN post_quality_dimensions q ON q.tweet_id=p.tweet_id
          WHERE p.posted_at>=? AND p.posted_at<?
            AND (q.tweet_id IS NULL OR (
              COALESCE(q.correction_required,0)=0
              AND COALESCE(q.manual_delete_required,0)=0
              AND COALESCE(q.anger_score,0)<7
              AND COALESCE(q.trust_score,10)>=5))
          ORDER BY impressions DESC,p.id DESC LIMIT 10""",
          (week.isoformat(), end.isoformat()))]


def _shorts_text(index: int, theme: dict) -> str:
    topic = theme.get("topic_key") or "今週の政治・行政テーマ"
    tweet_id = theme.get("tweet_id") or ""
    return f"""# Shorts {index:02d}

## タイトル案
60秒で整理する「{topic}」

## 想定尺
45〜60秒

## 冒頭フック
この論点で確認すべきなのは、感情的な賛否より制度と責任の所在です。

## ナレーション台本
この台本はAIが作成した下書きです。公開前に人間が一次資料を確認してください。
今週注目されたテーマは「{topic}」でした。
まず、確認できた事実と、まだ確認できていない主張を分けます。
次に見るべきなのは、誰が決定し、どの予算と権限を使い、結果を誰が検証するのかです。
賛成側の最も強い根拠と、反対側の最も強い懸念を同じ基準で比較します。
結論は、一次資料と公式説明を確認したうえで判断する必要があります。

## 画面テキスト
- 事実と意見を分離
- 予算・権限・責任
- 賛否の最強論拠を比較

## 必要な図表・一次資料
- 政府・国会・自治体の一次資料
- 予算、法案、統計の該当箇所
- 決定過程を示す時系列

## 事実確認済みポイント
- 元データは公開済みX投稿と週次レポートのテーマ分類
- 詳細数値は公開前に一次資料で再確認する

## 注意すべき表現
- 未確認情報を断定しない
- 個人攻撃、属性攻撃、過度な怒り表現を使わない
- 他人の投稿本文をコピーしない

## Xで根拠となった投稿ID
{tweet_id}

## note記事への展開可能性
制度背景、賛否の論拠、一次資料を追加して長文化可能
"""


def _note_text(index: int, theme: dict) -> str:
    topic = theme.get("topic_key") or "今週の政治・行政テーマ"
    tweet_id = theme.get("tweet_id") or ""
    return f"""# note記事案 {index:02d}

## タイトル案
「{topic}」を制度と説明責任から読み解く

## 想定読者
政治ニュースを事実と制度から落ち着いて確認したい読者

## 無料・有料の提案
基本的な事実整理は無料。一次資料比較と詳細な論点表は有料候補。

## 導入
この記事はAIが作成した下書きです。公開前に人間が一次資料と引用範囲を確認します。
今週の「{topic}」を、賛否の応援合戦ではなく制度・予算・権限・説明責任から整理します。

## 事実関係
公開時点を明記し、政府・国会・自治体などの一次資料から確認できる事項だけを記載する。

## 制度・政策の背景
意思決定主体、法的根拠、予算、対象者、検証期限を整理する。

## 賛成側の強い主張
政策目的、期待される便益、代替案より優れるとする根拠を最も強い形で示す。

## 反対側の強い主張
費用、副作用、権限集中、実施可能性、検証不足に関する最も強い懸念を示す。

## 久世ゆいの評価
政策目的ではなく、手段の妥当性、費用、透明性、検証可能性を基準に評価する。

## 読者が見るべきポイント
- 誰が決めたか
- 費用を誰が負担するか
- 成果指標と見直し条件があるか
- 反対意見への公式回答があるか

## 一次資料一覧
- 公開前に政府・国会・自治体等のURLと資料名を人間が追記

## X投稿からの再利用箇所
- テーマ分類のみ再利用。投稿本文は転載しない。
- 根拠投稿ID: {tweet_id}

## ファクトチェック項目
- 日付、数値、法的状態、決定主体、引用の文脈

## 公開前の人間確認事項
- 一次資料の再確認
- AI生成である旨の表示
- 誹謗中傷、著作権、誤認可能性の確認
- 自動公開は禁止
"""


def build_content_pipeline(
    week_start: str | None = None,
    *,
    path: Path | None = None,
) -> dict:
    if os.environ.get("CONTENT_PIPELINE_ENABLED", "true").lower() not in {
        "1", "true", "yes",
    }:
        return {"status": "disabled", "published": False}
    path = path or db_path()
    apply_additive_migrations(path)
    if forecast(path).get("restriction_level", 0) >= 1:
        return {
            "status": "budget_restricted",
            "reason": "content_pipeline_is_first_degradation_step",
            "published": False,
            "shorts": [],
            "note_articles": [],
        }
    week = _week_start(week_start)
    source = _weekly_source(week)
    themes = _safe_themes(week, path)
    if not themes:
        return {
            "status": "insufficient_data", "week_start": week.isoformat(),
            "shorts": [], "note_articles": [], "published": False,
        }

    budget = max(0.0, float(os.environ.get(
        "CONTENT_PIPELINE_WEEKLY_BUDGET_USD", "0.40")))
    shorts_max = min(3, max(0, int(os.environ.get(
        "CONTENT_PIPELINE_SHORTS_MAX_PER_WEEK", "3"))))
    note_max = min(2, max(0, int(os.environ.get(
        "CONTENT_PIPELINE_NOTE_MAX_PER_WEEK", "2"))))
    # Local deterministic drafts cost no API budget. If the configured allowance
    # is very small, still reduce volume as an operational safety signal.
    if budget < 0.20:
        note_max = min(note_max, 1)
    if budget < 0.10:
        shorts_max = min(shorts_max, 1)

    output = _root() / "outputs" / "content_pipeline" / week.isoformat()
    shorts_dir = output / "shorts"
    note_dir = output / "note"
    shorts_dir.mkdir(parents=True, exist_ok=True)
    note_dir.mkdir(parents=True, exist_ok=True)
    shorts = []
    notes = []
    for index, theme in enumerate(themes[:shorts_max], start=1):
        file = shorts_dir / f"shorts_{index:02d}.md"
        file.write_text(_shorts_text(index, theme), encoding="utf-8")
        shorts.append(str(file))
    for index, theme in enumerate(themes[:note_max], start=1):
        file = note_dir / f"note_{index:02d}.md"
        file.write_text(_note_text(index, theme), encoding="utf-8")
        notes.append(str(file))
    manifest = {
        "week_start": week.isoformat(),
        "generated_at": datetime.now(JST).isoformat(),
        "source_weekly_report": str(source or ""),
        "shorts": shorts,
        "note_articles": notes,
        "openai_cost_usd": 0.0,
        "generation_mode": "local_safe_draft",
        "status": "draft",
        "published": False,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write("""INSERT INTO content_pipeline_runs
      (week_start,generated_at,source_weekly_report,shorts_json,note_articles_json,
       openai_cost_usd,status,manifest_path,metadata_json)
      VALUES (?,?,?,?,?,?,?,?,?)
      ON CONFLICT(week_start) DO UPDATE SET generated_at=excluded.generated_at,
       source_weekly_report=excluded.source_weekly_report,
       shorts_json=excluded.shorts_json,note_articles_json=excluded.note_articles_json,
       openai_cost_usd=excluded.openai_cost_usd,status=excluded.status,
       manifest_path=excluded.manifest_path,metadata_json=excluded.metadata_json""", (
        manifest["week_start"], manifest["generated_at"],
        manifest["source_weekly_report"], json.dumps(shorts, ensure_ascii=False),
        json.dumps(notes, ensure_ascii=False), 0.0, "draft",
        str(manifest_path), json.dumps({
            "published": False, "generation_mode": "local_safe_draft",
            "filtered_theme_count": len(themes),
        }, ensure_ascii=False),
    ), path)
    return {**manifest, "manifest_path": str(manifest_path)}
