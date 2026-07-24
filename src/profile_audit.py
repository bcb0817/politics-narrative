"""Read-only profile transparency checklist generator."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def run(root: Path) -> Path:
    bio = os.environ.get("X_PROFILE_BIO", "")
    checks = [
        ("AI政治ニュース解説キャラクターの明示", any(v in bio for v in ("AI", "人工知能"))),
        ("一部が自動生成・自動投稿であることの明示", any(v in bio for v in ("自動生成", "自動投稿"))),
        ("運営者・人間管理アカウントへの接続方法", any(v in bio for v in ("運営", "管理", "連絡"))),
        ("訂正依頼の導線", any(v in bio for v in ("訂正", "修正依頼"))),
        ("実在記者・政治家と誤認させない説明", any(v in bio for v in ("キャラクター", "AI"))),
        ("Xの自動アカウントラベルを人間が確認", False),
    ]
    path = root / "reports" / "profile_audit.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Xプロフィール監査", "", f"生成日時: {datetime.now(JST).isoformat()}", "",
             "コードからプロフィール変更は行っていません。", ""]
    lines.extend(f"- [{'x' if ok else ' '}] {label}" for label, ok in checks)
    lines += ["", "## 人間による確認", "", "- 実際のXプロフィール文と固定投稿を確認する",
              "- 自動アカウントラベルを設定・確認する", "- 訂正窓口が機能するか確認する"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
