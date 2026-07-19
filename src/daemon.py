import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent.parent
POST_SCRIPT = PROJECT_DIR / "src" / "post.py"
INTERVAL_SECONDS = 300
JST = ZoneInfo("Asia/Tokyo")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_DIR / "bot.log", encoding="utf-8"),
    ],
)

logger = logging.getLogger(__name__)

def run_bot():
    now = datetime.now(JST)
    logger.info("デーモン: %s JST に実行開始", now.strftime("%Y-%m-%d %H:%M:%S"))

    result = subprocess.run(
        [sys.executable, str(POST_SCRIPT)],
        cwd=PROJECT_DIR,
        check=False,
    )

    logger.info("デーモン: 実行終了（終了コード=%s）", result.returncode)

def main():
    logger.info("デーモンを開始しました")
    logger.info("プロジェクトディレクトリ: %s", PROJECT_DIR)
    logger.info("実行間隔: %s 秒", INTERVAL_SECONDS)

    while True:
        try:
            run_bot()
        except Exception:
            logger.exception("デーモンで予期しないエラーが発生しました")

        next_run = datetime.fromtimestamp(
            time.time() + INTERVAL_SECONDS,
            tz=JST,
        )
        logger.info(
            "デーモン: 次回確認は %s JST",
            next_run.strftime("%Y-%m-%d %H:%M:%S"),
        )
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("ユーザー操作によりデーモンを停止しました")
