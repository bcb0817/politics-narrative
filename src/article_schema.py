"""Small explicit schemas shared by REST requests and local validation."""
S = {'type': 'string'}
N = {'type': ['string', 'null']}
I = {'type': 'integer'}
B = {'type': 'boolean'}
def arr(item): return {'type': 'array', 'items': item}
def obj(**fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}

EVIDENCE = obj(source_id=I, quote=S)
PLAN = obj(reader_question=S, conclusion=S, facts=arr(obj(statement=S, evidence=arr(EVIDENCE))),
           analysis=arr(S), counterarguments=arr(obj(statement=S, evidence=arr(EVIDENCE))),
           unknowns=arr(S), exclusions=arr(obj(information=S, reason=S)),
           source_metadata=arr(obj(source_id=I, publisher=N, published_at=N, updated_at=N,
                                   event_date=N, kind=S, confirmed_scope=S, limitations=arr(S))))
CLAIM = obj(id=S, text=S, classification=S, severity=S, evidence=arr(EVIDENCE),
            criterion=N, limitations=arr(S))
ARTICLE = obj(titles=arr(S), recommended_title=S, body=S, promo_posts=arr(S), claims=arr(CLAIM))
ISSUE = obj(code=S, severity=S, claim_id=N, description=S, evidence=arr(EVIDENCE))
REVIEW = obj(claims=arr(CLAIM), issues=arr(ISSUE), coverage_complete=B, summary=S)
SEARCH = obj(urls=arr(S), limitations=arr(S))

def validate(value, schema, path='$'):
    kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    kind = ('null' if value is None else 'boolean' if isinstance(value, bool) else
            'integer' if isinstance(value, int) else 'string' if isinstance(value, str) else
            'array' if isinstance(value, list) else 'object' if isinstance(value, dict) else 'invalid')
    if kind not in kinds: raise ValueError('schema_type:' + path)
    if kind == 'object':
        if set(value) != set(schema['required']): raise ValueError('schema_fields:' + path)
        for key, child in value.items(): validate(child, schema['properties'][key], path+'.'+key)
    if kind == 'array':
        for i, child in enumerate(value): validate(child, schema['items'], path+f'[{i}]')
