name: X Auto Post Bot

on:
  schedule:
    - cron: '0 22 * * *'
    - cron: '30 22 * * *'
    - cron: '0 23 * * *'
    - cron: '30 23 * * *'
    - cron: '0 0 * * *'
    - cron: '0 3 * * *'
    - cron: '15 3 * * *'
    - cron: '30 3 * * *'
    - cron: '45 3 * * *'
    - cron: '0 4 * * *'
    - cron: '0 9 * * *'
    - cron: '15 9 * * *'
    - cron: '30 9 * * *'
    - cron: '45 9 * * *'
    - cron: '0 10 * * *'
    - cron: '15 10 * * *'
    - cron: '30 10 * * *'
    - cron: '45 10 * * *'
    - cron: '0 11 * * *'
    - cron: '15 11 * * *'
    - cron: '30 11 * * *'
    - cron: '45 11 * * *'
    - cron: '0 12 * * *'
    - cron: '15 12 * * *'
    - cron: '30 12 * * *'
    - cron: '45 12 * * *'
    - cron: '0 13 * * *'
    - cron: '15 13 * * *'
    - cron: '30 13 * * *'
    - cron: '45 13 * * *'
  workflow_dispatch:
    inputs:
      mode:
        description: '投稿モード（link / normal / test）'
        required: false
        default: 'test'

jobs:
  post:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Restore posted URLs cache
        uses: actions/cache@v4
        with:
          path: src/posted_urls.json
          key: posted-urls-${{ github.run_id }}
          restore-keys: |
            posted-urls-

      - name: Determine post mode
        id: mode
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            echo "mode=${{ github.event.inputs.mode }}" >> $GITHUB_OUTPUT
          else
            HOUR=$(date -u +%H)
            MINUTE=$(date -u +%M)
            if ( [ "$HOUR" = "22" ] || [ "$HOUR" = "03" ] || [ "$HOUR" = "09" ] ) && [ "$MINUTE" = "00" ]; then
              echo "mode=link" >> $GITHUB_OUTPUT
            else
              echo "mode=normal" >> $GITHUB_OUTPUT
            fi
          fi

      - name: Post to X
        env:
          API_KEY: ${{ secrets.API_KEY }}
          API_KEY_SECRET: ${{ secrets.API_KEY_SECRET }}
          ACCESS_TOKEN: ${{ secrets.ACCESS_TOKEN }}
          ACCESS_TOKEN_SECRET: ${{ secrets.ACCESS_TOKEN_SECRET }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          cd src
          python post.py ${{ steps.mode.outputs.mode }}

      - name: Save posted URLs cache
        uses: actions/cache/save@v4
        if: always()
        with:
          path: src/posted_urls.json
          key: posted-urls-${{ github.run_id }}
