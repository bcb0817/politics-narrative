"""Evidence consistency checks; semantic fact checking still requires human review."""
import re
from .article_schema import ARTICLE, REVIEW, validate

def issue(code,description,severity='major',claim_id=None):
    return dict(code=code,severity=severity,claim_id=claim_id,description=description,evidence=[])

def evidence_errors(claim,sources):
    errors=[]; by_id={s['id']:s for s in sources}
    evidence=claim.get('evidence',[])
    if not evidence: errors.append(issue('missing_evidence','根拠箇所がありません',claim_id=claim.get('id')))
    for e in evidence:
        src=by_id.get(e['source_id'])
        if not src or src.get('error') or not e['quote'].strip() or e['quote'] not in src.get('text',''):
            errors.append(issue('invalid_evidence','取得済み資料の原文に一致しません','critical',claim.get('id')))
    return errors

def semantic_guards(claim,sources):
    """Conservative known-confusion flags, never an automatic truth classifier."""
    text=claim['text']; quote='\n'.join(e['quote'] for e in claim.get('evidence',[])); errors=[]
    checks=[
        ('policy_stage',r'施行(?:された|した|済み|されています)',r'法案.{0,16}提出|提出.{0,12}法案','法案提出と施行の混同'),
        ('budget_period',r'年間.{0,15}\d|毎年.{0,15}\d',r'\d+年間.{0,20}(総額|合計)|複数年|総額.{0,10}\d+年間','複数年総額を年間額へ置換'),
        ('judicial_stage',r'有罪(?:が確定|判決|になった|である|だ)',r'起訴|公判前','起訴と有罪の混同'),
        ('title_at_event',r'現職|現在.{0,15}(大臣|知事|議員)',r'元職|退任|元大臣|元知事','出来事時点の肩書の確認が必要'),
        ('fabricated_public_opinion',r'国民(全体|の総意|全員)|世論は一致',r'X投稿|SNS|投稿者','SNS反応は国民全体の意見ではない'),
    ]
    for code,pattern,anchor,description in checks:
        if re.search(pattern,text) and re.search(anchor,quote): errors.append(issue(code,description,'critical',claim.get('id')))
    if re.search(r'条件|場合|ただし|とは限らない',quote) and not re.search(r'条件|場合|ただし|未|可能|限らない',text):
        errors.append(issue('missing_context','根拠にある条件・留保が主張から脱落していないか確認','major',claim.get('id')))
    return errors

def check(article,sources,review=None,min_chars=2500,max_chars=4000):
    errors=[]
    try: validate(article,ARTICLE)
    except ValueError: return [issue('article_schema','必須フィールド・型が不正','critical')]
    if len(article['titles'])!=3 or len(article['promo_posts'])!=2 or article['recommended_title'] not in article['titles']:
        errors.append(issue('output_cardinality','タイトル3案、推奨タイトル、紹介文2案が必要'))
    if not min_chars<=len(article['body'])<=max_chars:
        errors.append(issue('body_length',f'本文文字数={len(article["body"])}、指定範囲外','minor'))
    if not article['body'].strip() or not article['claims']:
        errors.append(issue('empty_article','本文または主張一覧が空','critical'))
    text=article['body']+'\n'+'\n'.join(article['titles']+article['promo_posts'])
    refs={int(x) for x in re.findall(r'\[(\d+)\]',text)}
    valid={s['id'] for s in sources if not s.get('error')}
    if not refs or refs-valid: errors.append(issue('source_numbers','出典番号の欠落・不正','critical'))
    if any(s.get('error') for s in sources): errors.append(issue('source_fetch_failed','取得失敗資料あり。追加調査または入力の見直しが必要'))
    if not any(s.get('kind')=='primary' for s in sources): errors.append(issue('no_primary_source','確認できた一次資料がありません'))
    ids=[c['id'] for c in article['claims']]
    if len(ids)!=len(set(ids)): errors.append(issue('duplicate_claim_ids','主張IDが重複','critical'))
    for claim in article['claims']:
        if not claim['text'].strip() or claim['text'] not in text: errors.append(issue('claim_not_in_article','主張が出力本文・見出し・紹介文に一致しない',claim_id=claim['id']))
        if claim['severity'] not in ('critical','major','minor'):
            errors.append(issue('claim_severity','主張の重大度が不正','major',claim['id']))
        errors+=evidence_errors(claim,sources)+semantic_guards(claim,sources)
        kind=claim['classification']
        if kind not in ('supported','analysis','insufficient','contradicted'):
            errors.append(issue('classification','不正な主張分類','major',claim['id']))
        if kind in ('insufficient','contradicted'):
            errors.append(issue(kind,'裏付け不足または資料と矛盾',claim['severity'] if claim['severity'] in ('critical','major','minor') else 'major',claim['id']))
        if kind=='analysis' and (not claim.get('criterion') or not re.search('評価|意見|考え|判断|分析',claim['text'])):
            errors.append(issue('unlabelled_opinion','分析・評価の明示または判断基準が不足',claim_id=claim['id']))
    # Mechanical checks cover only obvious headline overstatement, not all semantics.
    if any(re.search('必ず|確実|完全|絶対',t) for t in article['titles']) and re.search('未定|可能性|案|検討',article['body']):
        errors.append(issue('headline_overclaim','本文の不確実性と強い見出しが整合しない','critical'))
    if re.search('ネット騒然|完全論破|売国',text): errors.append(issue('inflammatory_style','禁止された煽り表現'))
    if review is not None:
        try: validate(review,REVIEW)
        except ValueError: return errors+[issue('review_schema','検証応答の形式が不正','critical')]
        if not review['coverage_complete'] or not set(ids).issubset({c['id'] for c in review['claims']}):
            errors.append(issue('review_incomplete','独立検証が全主張を網羅していません','critical'))
        for claim in review['claims']:
            original=next((c for c in article['claims'] if c['id']==claim['id']),None)
            if not claim['text'].strip() or claim['text'] not in text or (original and original['text']!=claim['text']):
                errors.append(issue('review_claim_mismatch','検証主張が本文または元の主張IDと一致しない','critical',claim['id']))
            if claim['classification'] not in ('supported','analysis','insufficient','contradicted') or claim['severity'] not in ('critical','major','minor'):
                errors.append(issue('review_classification','検証主張の分類・重大度が不正','critical',claim['id']))
            if claim['classification']=='analysis' and (not claim.get('criterion') or not re.search('評価|意見|考え|判断|分析',claim['text'])):
                errors.append(issue('unlabelled_opinion','検証で評価とされた主張の明示または判断基準が不足',claim_id=claim['id']))
            errors+=evidence_errors(claim,sources)+semantic_guards(claim,sources)
            if claim['classification'] in ('insufficient','contradicted'):
                errors.append(issue(claim['classification'],claim['text'],claim['severity'],claim['id']))
        for problem in review['issues']:
            if problem['severity'] not in ('critical','major','minor'):
                errors.append(issue('review_severity','不正な重大度','critical')); continue
            # Unsupported accusations by a reviewer are themselves unresolved, never silently accepted.
            if problem['evidence']:
                errors+=evidence_errors(problem,sources)
            errors.append(problem)
    return errors

def blocking(issues): return any(x['severity'] in ('critical','major') for x in issues)
