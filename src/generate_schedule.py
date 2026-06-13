import random
import re

def generate_crons():
    crons = []
    
    # 朝 6時〜9時 JST = UTC 21時〜0時 13回
    morning_slots = []
    for minute in range(0, 180):  # 180分間
        morning_slots.append((21 * 60 + minute) % (24 * 60))
    selected = sorted(random.sample(morning_slots, 13))
    for m in selected:
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")
    
    # 昼 11時〜13時 JST = UTC 2時〜4時 4回
    noon_slots = list(range(2 * 60, 4 * 60))
    selected = sorted(random.sample(noon_slots, 4))
    for m in selected:
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")
    
    # 夜 17時〜22時 JST = UTC 8時〜13時 13回
    evening_slots = list(range(8 * 60, 13 * 60))
    selected = sorted(random.sample(evening_slots, 13))
    for m in selected:
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")
    
    return crons

def update_post_yml(crons):
    with open('.github/workflows/post.yml', 'r') as f:
        content = f.read()
    
    # scheduleブロックを新しいcronで置き換え
    cron_lines = '\n'.join([f'    - cron: {c}' for c in crons])
    new_schedule = f'  schedule:\n{cron_lines}'
    
    new_content = re.sub(
        r'  schedule:.*?(?=  workflow_dispatch:)',
        new_schedule + '\n',
        content,
        flags=re.DOTALL
    )
    
    with open('.github/workflows/post.yml', 'w') as f:
        f.write(new_content)
    
    print(f"スケジュール更新完了！{len(crons)}個のcronを設定しました")
    for c in crons:
        print(f"  - cron: {c}")

if __name__ == "__main__":
    crons = generate_crons()
    update_post_yml(crons)
