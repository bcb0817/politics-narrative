"""One-angle editing and evidence checks shared by generation and publication."""
import re
import hashlib
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from urllib.parse import urlsplit
from .article_schema import obj, arr, S, I, B
from .article_sources import fetch

FACT = obj(text=S, segment_ids=arr(I))
DRAFT = obj(text=S, decision=S, skip_reason=S, mode=S, angle=S, form=S,
    facts=arr(FACT), context=obj(entities=arr(S), dates=arr(S), quantities=arr(S), stage=S, unknowns=arr(S)),
    claims=arr(obj(text=S, kind=S, criterion=S, segment_ids=arr(I))))
DIMENSION = obj(pass_check=B, reason=S)
REVIEW = obj(approved=B, coverage_complete=B, attribution_ok=B, conditions_preserved=B,
    opinion_separated=B, no_group_attack=B,
    checks=arr(obj(claim_index=I, supported=B, segment_ids=arr(I))), issues=arr(S),
    quality=obj(accuracy=DIMENSION, specificity=DIMENSION, readability=DIMENSION,
                editorial_value=DIMENSION, repetition=DIMENSION),
    comparison=obj(same_news=B, same_angle=B, repeated_style=B, new_fact=B,
                   new_fact_text=S, segment_ids=arr(I), compared_ids=arr(S), reason=S))

REVIEW_TASK = '''独立審査。取得本文の段落だけを根拠とし、関連記事・見出し・モデルの同意を裏付けにしない。
factは根拠事実、opinion/inferenceは判断基準と根拠に基づく評価・推論として検証。
supportedは思想への賛同ではない。意見の形を借りた数字・因果・犯罪・動機の断定も検証する。
全主張の網羅、人物・日付・単位・条件・引用、選挙/捜査/予算/政策の段階を照合。
意見が明示ラベルなしでも語調で分かり報道の見解と混同されなければopinion_separated=true。
移民政策批判は許容し、出自集団への攻撃や犯罪一般化はno_group_attack=false。
qualityは正確性/記事固有の具体性/読みやすさ/編集価値/反復を別々に判定し各reasonを具体化。
特に「説明責任が重要」「数字を示すべき」「評価の基準とする」だけで、記事固有の条件や差分を指摘しない論評はeditorial_value.pass_check=false。
国民負担など評価軸の宣言をするだけの結びは削除対象。具体的な事実速報に戻す方がよい場合も記録する。
単なる皮肉・疑問文・口語は加点しない。news_briefは新しい具体的事実を簡潔に伝える価値で評価できる。
recent_posts全件と同じニュース、同じ論点、同じ型を別々に比較する。
comparison.compared_idsには渡された全IDを列挙。新しい事実があれば本文に含むその部分文字列と根拠段落を記録。
語順変更、別人物の同じ要求、取得日更新だけを新事実としない。続報の決定/条件/行動の変化は新事実になり得る。
似た語句でも新事実があり必要な語句なら機械的に拒否しない。不要な定型締めや無関係な比喩はquality.repetition.pass_check=false。
根拠不足はaccuracy=falseにしてissuesへ。claim_indexは0始まり。内部項目名を本文へ出さない。'''

CAUTIONS = ('これは単なる','本質は','問われているのは','今後の動向に注目',
            '議論を呼びそう','私たち一人ひとり','皆さんはどう思いますか','つまり','一方で','ただし')

def parts(source):
    return {str(i):s for i,s in enumerate(source['text'].splitlines(),1) if s.strip()}

def fetch_news(url, source_id, max_chars=7000):
    # Existing public-IP/redirect/byte limits remain authoritative. Never log in/bypass.
    source=fetch(url,source_id,max_chars=max_chars)
    return article_excerpt(source)

def article_excerpt(source):
    """Also cleans saved legacy source snapshots without network access."""
    source=dict(source,limitations=list(source.get('limitations',[])))
    url=source.get('url','')
    if source.get('error') or urlsplit(url).hostname!='news.web.nhk': return source
    if source.get('confirmed_scope','').startswith('visible_article_excerpt_only'): return source
    lines=source['text'].splitlines()
    start=next((i for i,s in enumerate(lines) if re.fullmatch(r'\d{4}年\d+月\d+日.*\d+:\d+.*',s)),None)
    end=next((i for i,s in enumerate(lines) if start is not None and i>start and s.strip() in ('注目ワード','あわせて読みたい','関連記事')),None)
    if start is None or end is None:
        source.update(text='',error='article_boundary_unconfirmed')
        return source
    body=[s for s in lines[start+1:end] if s.strip() and not s.startswith('シェアする') and not re.fullmatch(r'\(\d{4}年.*更新\)',s)]
    # Retain visible evidence, mark truncated clauses; never fetch hidden/full content.
    cleaned=[]
    for line in body:
        if line.endswith(('…','...')):
            line=line.rstrip('….')
            source['limitations'].append('public_excerpt_ends_mid_sentence; do_not_complete_or_infer_missing_conditions')
        if line: cleaned.append(line)
    source['text']='\n'.join(cleaned)
    source['article_excerpt_sha256']=hashlib.sha256(source['text'].encode()).hexdigest()
    source['confirmed_scope']='visible_article_excerpt_only; related links/navigation excluded'
    source['limitations'].append('not_full_article; no_access_restrictions_bypassed')
    if not source['text']: source['error']='article_body_unavailable'
    return source

def normalized(text):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',text))

def numbers(text):
    return set(re.findall(r'\d+(?:\.\d+)?',normalized(text).replace(',','')))

def stage_errors(claim,evidence):
    # Narrow affirmative patterns only; semantic review still handles negation,
    # actors and context. These guards do not prove a matched claim is correct.
    if claim['kind']!='fact': return []
    text=claim['text']; errors=[]
    for pattern,terms in (
        (r'施行(?:された|済み|開始)',('施行',)),
        (r'有罪(?:が確定|判決|となった)',('有罪',)),
        (r'正式に届け出|立候補を届け出',('届け出','届出')),
        (r'予算(?:が|は)成立',('成立',)),
        (r'予算(?:が|は)執行',('執行',))):
        if re.search(pattern,text) and not any(term in evidence for term in terms): errors.append('stage_not_in_evidence')
    if re.search(r'年額|年間',text) and re.search(r'複数年|\d+年(?:間)?総額',evidence) and not re.search(r'年額|年間\d',evidence):
        errors.append('period_mismatch')
    return errors

def style_signals(text,history):
    """Diagnostic hints only; neither phrase presence nor similarity rejects alone."""
    return dict(caution_phrases=[s for s in CAUTIONS if s in text],
        opening_matches=[r['id'] for r in history if normalized(text)[:12]==normalized(r['text'])[:12]],
        ending_matches=[r['id'] for r in history if normalized(text)[-12:]==normalized(r['text'])[-12:]],
        similar_text=[r['id'] for r in history if SequenceMatcher(None,text,r['text']).ratio()>.8])

def quality_errors(draft,review,source,history=()):
    errors=[]; p=parts(source)
    if draft['decision']!='publish': return ['editorial_skip']
    if draft['mode'] not in ('comment','news_brief') or not draft['angle'].strip() or not draft['form'].strip(): errors.append('missing_angle')
    if not draft['facts']: errors.append('no_extracted_facts')
    for fact in draft['facts']:
        if not fact['text'].strip() or not fact['segment_ids'] or any(str(i) not in p for i in fact['segment_ids']): errors.append('fact_evidence_missing')
    for claim in draft['claims']:
        evidence='\n'.join(p.get(str(i),'') for i in claim['segment_ids'])
        if numbers(claim['text'])-numbers(evidence): errors.append('unsupported_number')
        errors.extend(stage_errors(claim,evidence))
    # Full-text check catches numbers omitted from the claims inventory too.
    if numbers(draft['text'])-numbers(source['text']): errors.append('unsupported_number')
    for quote in re.findall(r'「([^」]+)」|“([^”]+)”',draft['text']):
        if normalized(''.join(quote)) not in normalized(source['text']): errors.append('unverified_quote')
    if re.search(r'(?:segment_ids|skip_reason|pass_check|same_news|criterion)\s*[:：=]',draft['text']): errors.append('internal_process_in_post')
    for axis,check in review['quality'].items():
        if not check['pass_check'] or not check['reason'].strip(): errors.append('quality_'+axis)
    comp=review['comparison']; known={r['id'] for r in history}
    if set(comp['compared_ids'])!=known: errors.append('history_coverage')
    same_url=any(r.get('url')==source.get('url') and source.get('url') for r in history)
    if (same_url or comp['same_news'] or comp['same_angle']) and not comp['new_fact']: errors.append('duplicate_without_new_fact')
    if comp['new_fact']:
        if not comp['new_fact_text'].strip() or comp['new_fact_text'] not in draft['text'] or not comp['segment_ids'] or any(str(i) not in p for i in comp['segment_ids']): errors.append('new_fact_evidence_missing')
        if not any(c['kind']=='fact' and comp['new_fact_text'] in c['text'] for c in draft['claims']): errors.append('new_fact_not_factual_claim')
        if comp['new_fact_text'] and any(normalized(comp['new_fact_text']) in normalized(r['text']) for r in history): errors.append('new_fact_already_posted')
    if any(normalized(draft['text'])==normalized(r['text']) for r in history): errors.append('exact_duplicate')
    return sorted(set(errors))

def audit(rows):
    """Read-only statistics, never infer reach or platform automation classification."""
    bodies=[r['body'] for r in rows if r.get('body')]
    return dict(count=len(bodies),dates=[r['created'] for r in rows],
        nhk_opening=sum(s.startswith('NHKによると、') for s in bodies),
        opinion_label=sum('【論評】' in s for s in bodies),
        lengths=[len(s) for s in bodies],paragraphs=[len(s.splitlines()) for s in bodies],
        phrases={p:sum(p in s for s in bodies) for p in CAUTIONS},
        endings=dict(Counter(s[-12:] for s in bodies)), reach_effect=None)
