#!/usr/bin/env python3
"""Validate the generated graph against the original inputs (no browser claims)."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
csv.field_size_limit(2**31-1)
index=json.loads((ROOT/'board/index.json').read_text());rows={r['id']:r for r in index['rows']}
audit=json.loads((ROOT/'board/audit.json').read_text())
assert len(rows)==len(index['rows'])
# Independently read every CSV, including the huge original expanded export.
for path,count in audit['inputs'].items():
    if path.endswith('.csv'):
        with (ROOT/path).open(encoding='utf-8-sig',newline='') as f:actual=sum(1 for _ in csv.DictReader((line.replace("\x00","\ufffd") for line in f)))
        assert actual==count,(path,actual,count)
print('PASS: every CSV input counted independently',flush=True)
seen=set();edges=Counter();paths=set();variants=0;problem=0
for p in sorted((ROOT/'board/details').glob('*.js')):
    d=json.loads(p.read_text().split('=',1)[1].strip().removesuffix(';'))
    for rid,detail in d.items():
        assert rid in rows and rid not in seen,rid
        seen.add(rid);row=rows[rid]
        assert detail['origins']
        for value in detail['local_references']:
            if value:
                assert not value.startswith(('http:','https:'))
                paths.add(value)
        for rel in detail['relations']:
            assert rel['id'] in rows,(rid,rel)
            assert rows[rel['id']]['company']==row['company'],(rid,rel)
            key=json.dumps([*sorted([rid,rel['id']]),rel['label'],rel['certainty'],rel['proof']],ensure_ascii=False)
            edges[hashlib.sha256(key.encode()).digest()]+=1
            if rel['label'].startswith('Možná'):assert rel['certainty']=='possible'
        if row['kind'] in ['pages','codes']:assert row['catalogs'],rid
        for cat in row['catalogs']:assert rows[cat]['kind']=='documents' and rows[cat]['company']==row['company']
        raw=detail['raw']
        if row['kind']=='products' and raw.get('reference_resolution_status')=='different_default_variant':
            assert row['code']==raw['article_code_index_hint']
            assert raw['article_code_observed']!=row['code'];variants+=1
        if detail['problem']:
            assert row['kind']=='unresolved' and not detail['text'];problem+=1
        if not detail['relations']:assert detail['unresolved_reason']
assert seen==set(rows),(len(seen),len(rows))
assert all(n==2 for n in edges.values()),Counter(edges.values())
assert len(edges)==audit['relationships']
for path in paths:assert (ROOT/path).is_file(),path
assert variants>0
assert problem>0
result=dict(records=len(rows),bidirectional_relationships=len(edges),local_files=len(paths),preserved_variant_mismatches=variants,quarantined_records=problem,status='PASS',visual_verification='Separate browser inspection required')
(ROOT/'board/verification.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result),flush=True)
