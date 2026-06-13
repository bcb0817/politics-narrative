import random
import os
import base64
import json
import urllib.request
import urllib.error

def generate_crons():
    crons = []

    # 朝 6時〜9時 JST（UTC 21時〜0時）13回
    morning = random.sample(range(21 * 60, 24 * 60), 13)
    for m in sorted(morning):
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h % 24} * * *'")

    # 昼 11時〜13時 JST（UTC 2時〜4時）4回
    noon = random.sample(range(2 * 60, 4 * 60), 4)
    for m in sorted(noon):
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")

    # 夜 17時〜22時 JST（UTC 8時〜13時）13回
    evening = random.sample(range(8 * 60, 13 * 60), 13)
    for m in sorted(evening):
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")

    return crons

def get_file_sha(token, repo, path):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    })
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())["sha"]

def update_file_via_api(token, repo, path, content, sha):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    data = json.dumps({
        "message": "Daily schedule reset",
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }, method="PUT")
    with urllib.request.urlopen(req) as res:
        print("更新成功！")

def build_post_yml(crons):
    cron_lines = "\n".join([f"    - cron: {c}" for c in crons])
    return f"""name: X Auto Post Bot

on:
  schedule:
{cron_lines}
  workflow_dispatch:
    inputs:
      mode:
        description: '投稿モード（link / normal / test）'
        required: false
        default: 'test'

jobs:
  post:
    runs-on: ubuntu-latest
    env:
      FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore posted URLs cache
        uses: actions/cache@v4
        with:
          path: src/posted_urls.json
          key: posted-urls-${{{{ github.run_id }}}}
          restore-keys: |
            posted-urls-

      - name: Determine post mode
        id: mode
        run: |
          if [ "${{{{ github.event_name }}}}" = "workflow_dispatch" ]; then
            echo "mode=${{{{ github.event.inputs.mode }}}}" >> $GITHUB_OUTPUT
          else
            HOUR=$(date -u +%H)
            if [ "$HOUR" = "21" ] || [ "$HOUR" = "22" ] || [ "$HOUR" = "02" ] || [ "$HOUR" = "08" ]; then
              echo "mode=link" >> $GITHUB_OUTPUT
            else
              echo "mode=normal" >> $GITHUB_OUTPUT
            fi
          fi

      - name: Post to X
        env:
          API_KEY: ${{{{ secrets.API_KEY }}}}
          API_KEY_SECRET: ${{{{ secrets.API_KEY_SECRET }}}}
          ACCESS_TOKEN: ${{{{ secrets.ACCESS_TOKEN }}}}
          ACCESS_TOKEN_SECRET: ${{{{ secrets.ACCESS_TOKEN_SECRET }}}}
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
        run: |
          cd src
          python post.py ${{{{ steps.mode.outputs.mode }}}}

      - name: Save posted URLs cache
        uses: actions/cache/save@v4
        if: always()
        with:
          path: src/posted_urls.json
          key: posted-urls-${{{{ github.run_id }}}}
"""

if __name__ == "__main__":
    token = os.environ["GH_PAT"]
    repo = "bcb0817/x-Politics-bot-Yui"
    path = ".github/workflows/post.yml"

    crons = generate_crons()
    content = build_post_yml(crons)
    sha = get_file_sha(token, repo, path)
    update_file_via_api(token, repo, path, content, sha)
    print(f"スケジュール更新完了！{len(crons)}個のcronを設定しました")
