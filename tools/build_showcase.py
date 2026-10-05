#!/usr/bin/env python3
"""Offline evidence graph. Never reads company board/data.js or changes the archive."""
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'board'
COMPANIES = {'AS-Creation': 'A.S. Création', 'Marburg': 'Marburg', 'Texdecor': 'Texdecor'}
csv.field_size_limit(2**31-1)

def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFD', str(s)).lower() if not unicodedata.combining(c))

def strings(v):
    if isinstance(v, dict):
        for x in v.values(): yield from strings(x)
    elif isinstance(v, list):
        for x in v: yield from strings(x)
    elif isinstance(v, str): yield v

def codekey(value):
    value=str(value).upper().strip()
    if value.startswith('AS'):
        value=value[2:]
        if value.endswith('-0'):value=value[:-2]
    return re.sub(r'[\s-]+','',value)

def decode(row):
    for k,v in row.items():
        if isinstance(v,str) and v.startswith(('[','{')):
            try: row[k]=json.loads(v)
            except ValueError: pass
    return row

def csv_rows(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        yield from (decode(r) for r in csv.DictReader((line.replace('\x00','\ufffd') for line in f)))

def json_rows(path):
    """Stream pretty-printed source arrays; bound even corrupt binary text fields.

    Oversized string fields keep a locator and are explicitly quarantined. Their
    original bytes stay in the archive. Ordinary records are parsed unchanged.
    """
    if path.stat().st_size < 40_000_000:
        value=json.loads(path.read_text())
        if isinstance(value,list): yield from value
        else: yield {'value':value}
        return
    with path.open() as f:
        buf=[]; dropped=[]
        while True:
            line=f.readline(262144)
            if not line: break
            if not line.endswith('\n') and len(line)==262144:
                field=re.match(r'\s*"([^"]+)"\s*:',line)
                if not field: raise ValueError(f'Unexpected oversized JSON line: {path}')
                tail=line
                while not tail.endswith('\n'):
                    tail=f.readline(262144)
                    if not tail: break
                comma=',' if tail.rstrip().endswith(',') else ''
                line='    '+json.dumps(field[1])+': ""'+comma+'\n'
                dropped.append(field[1])
            if line.startswith('  {'): buf=[line];dropped=[]
            elif buf:
                buf.append(line)
                if line.startswith('  }'):
                    r=json.loads(''.join(buf).rstrip().rstrip(','))
                    if dropped:r['_quarantined_fields']=dropped
                    yield r;buf=[]
        if buf: raise ValueError(f'Unfinished JSON record: {path}')

MONTHS=[['january','janvier','januar','enero','leden'],['february','fevrier','februar','febrero','unor'],['march','mars','marz','marzo','brezen'],['april','avril','abril','duben'],['may','mai','mayo','kveten'],['june','juin','juni','junio','cerven'],['july','juillet','juli','julio','cervenec'],['august','aout','agosto','srpen'],['september','septembre','septiembre','zari'],['october','octobre','oktober','octubre','rijen'],['november','novembre','noviembre','listopad'],['december','decembre','dezember','diciembre','prosinec']]
M={w:i+1 for i,ws in enumerate(MONTHS) for w in ws}
M.update({w:i for i,ws in enumerate(['jan','feb fevr','mar','apr avr','may','jun','jul','aug','sep sept','oct okt','nov','dec dez'],1) for w in ws.split()})
DATE_WORD=re.compile(r'\b(?:(\d{1,2})\s+)?('+ '|'.join(M)+r')\.?\s+((?:19|20)\d{2})\b',re.I)
DATE_NUM=re.compile(r'(?<!\d)(?:(\d{1,2})[./-])?(0?[1-9]|1[0-2])[./-]((?:19|20)\d{2})(?!\d)')
ISO=re.compile(r'(?<!\d)((?:19|20)\d{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?')

def dates(text,source,page=None,meaning='date_in_text'):
    out=[]; normalized=norm(text)
    for pattern in [DATE_WORD,DATE_NUM,ISO]:
        for m in pattern.finditer(normalized):
            if pattern is ISO: year,month,day=m.groups();month=int(month)
            else:
                day,month,year=m.groups(); month=M[month] if pattern is DATE_WORD else int(month)
            if day and not 1<=int(day)<=31:continue
            context=normalized[max(0,m.start()-65):m.end()+65]
            kind=meaning
            if meaning=='date_in_text':
                if re.search(r'expir|valid until|validite|valable jusqu|validity|valid to',context):kind='validity'
                elif re.search(r'revis|revision|mise a jour|updated|version',context):kind='revision'
                elif re.search(r'edition|publie|published|publication|imprim',context):kind='publication'
                elif day or pattern is ISO:kind='date_mention'
                elif re.search(r'\b(?:norme|standard|nf en|iso|astm|reglement|regulation)\b',context):kind='reference_date'
                else:kind='document_date'  # visible month imprint; never a product launch date
            out.append(dict(value=f'{year}-{month:02d}'+(f'-{int(day):02d}' if day else ''),precision='day' if day else 'month',meaning=kind,raw=text[m.start():m.end()],source=source,page=page,context=text[max(0,m.start()-65):m.end()+65].strip()))
    return out

def populate_collection_filters(rows):
    """A page/image can be filtered by its documented catalogue's collection.
    Never infer a product's collection from other products in the same PDF.
    """
    doc_collections=defaultdict(set)
    for row in rows:
        if row['kind']=='collections':
            for cid in row['catalogs']:doc_collections[cid].add(row['id'])
    for row in rows:
        if row['kind'] in ['documents','pages','images','codes']:
            row['collections']=sorted(set(row['collections']).union(*(doc_collections[cid] for cid in row['catalogs'])))


def build():
    OUT.mkdir(exist_ok=True);(OUT/'details').mkdir(exist_ok=True)
    rows=[]; details={}; byid={}; urlmap=defaultdict(set); files=defaultdict(set); audit={}; input_counts={}
    edges=set()
    def local(c,p):
        if not p or not isinstance(p,str) or p.startswith(('http:','https:')):return ''
        return p if p.startswith(c+'/') else c+'/'+p
    def add(c,kind,raw,origin,key=None):
        key=str(key or raw.get('id') or hashlib.sha256((origin+json.dumps(raw,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()[:24])
        rid=f'{c}:{kind}:{key}'
        if rid in byid:
            details[rid]['origins'].append(origin);return byid[rid]
        problem=bool(raw.get('_quarantined_fields'))
        txt=' '.join(str(raw.get(k) or '') for k in ['text','description','material_description'])
        if txt.lstrip().startswith(('%PDF','PK\x03','\x89PNG')) or '\x00' in txt[:2000] or txt.count('\ufffd')>30:problem=True
        if problem:txt=''
        brand=raw.get('scope_brand') or ' / '.join(raw.get('scope_brands') or []) or raw.get('brand') or COMPANIES[c]
        r=dict(id=rid,kind=kind,company=c,brand=brand,name=str(raw.get('name') or raw.get('title') or raw.get('article_code_raw') or raw.get('url') or Path(origin.split('#')[0]).name)[:500],code=raw.get('article_code') or raw.get('article_code_raw') or raw.get('code') or '',image=local(c,raw.get('image_local') or raw.get('thumb') or (raw.get('local_file') if kind=='images' else '')),years=[],catalogs=[],collections=[],dateValues=[],evidence='problem' if problem or raw.get('error') else 'standalone',search='')
        status='Problematický podklad — binární nebo nadměrný text; originál zachován' if problem else raw.get('details_status') or raw.get('status') or raw.get('evidence_type') or raw.get('extraction_status') or 'Samostatný archivní záznam'
        d=dict(raw=raw if not problem else {k:v for k,v in raw.items() if k not in ['text','description','material_description']},origins=[origin],text=txt,properties=raw.get('properties') or {},dates=[],relations=[],status=str(status),file=local(c,raw.get('local_file') or raw.get('source_pdf')),preview=local(c,raw.get('preview') or raw.get('image_local') or raw.get('local_file') if kind=='images' else raw.get('preview') or raw.get('image_local')),url=raw.get('url') or raw.get('source_url') or '',page=raw.get('page'),problem=problem)
        # Searchable text is separated from raw details and includes all ordinary text.
        r['search']=norm(' '.join(dict.fromkeys(re.findall(r'\S+', ' '.join([r['name'],str(r['code']),brand,txt,json.dumps(d['properties'],ensure_ascii=False)])))))
        if problem:r['kind']='unresolved'
        for field,meaning in [('retrieved_at','download'),('captured','archive'),('archive_capture','archive'),('website_lastmod','website_revision')]:
            value=str(raw.get(field) or '')
            if re.match(r'^\d{14}$',value):value=f'{value[:4]}-{value[4:6]}-{value[6:8]}'
            d['dates'].extend(dates(value,origin,meaning=meaning))
        if kind in ['brochures','history','collections','sources'] and not problem:
            for field in ['text','description','material_description','title','name','observed_date_labels']:
                for t in strings(raw.get(field,'')): d['dates'].extend(dates(t,origin,meaning='publisher_label' if kind=='brochures' else 'article_date' if kind=='history' else 'date_in_text'))
            if raw.get('year'):d['dates'].append(dict(value=str(raw['year']),precision='year',meaning='article_date' if kind=='history' else 'index_period',raw=str(raw['year']),source=origin,page=None))
        rows.append(r);byid[rid]=r;details[rid]=d
        for k in ['url','original_url','document_url']:
            v=raw.get(k)
            if isinstance(v,str):
                for u in v.split(' | '):
                    if u.startswith('http'):urlmap[c,u].add(rid)
        for a in raw.get('aliases') or []:
            if isinstance(a,dict):
                for k in ['url','viewer_url']:
                    if a.get(k):urlmap[c,a[k]].add(rid)
        for k in ['local_file','detail_local_file','source_pdf']:
            p=local(c,raw.get(k))
            if p:files[p].add(rid)
        return r
    def edge(a,b,label,certainty='confirmed',proof=''):
        if not a or not b or a==b:return
        a=a['id'] if isinstance(a,dict) else a;b=b['id'] if isinstance(b,dict) else b
        if a not in byid or b not in byid:return
        k=(min(a,b),max(a,b),label,certainty,proof)
        if k in edges:return
        edges.add(k)
        for x,y in [(a,b),(b,a)]:details[x]['relations'].append(dict(id=y,label=label,certainty=certainty,proof=proof))
    mapping={'products':'products','documents':'documents','pages':'pages','embedded_images':'images','code_evidence':'codes','digital_brochures':'brochures','collections_original':'collections','collections_expanded':'collections','trade_press_index':'history','trade_press_full_text':'history','historical_events':'history','failed_downloads':'unresolved','web_media_sources':'images'}
    docmap={};pagemap={}
    for c in COMPANIES:
        print('Import',c,flush=True)
        for p in sorted((ROOT/c/'research/exports').glob('*.csv')):
            kind=mapping.get(p.stem,'sources');count=0
            # JSON equivalent can safely skip oversized binary text fields while streaming.
            source=ROOT/c/'research/collections_expanded.json' if p.stem=='collections_expanded' else p
            iterator=json_rows(source) if source.suffix=='.json' else csv_rows(source)
            for i,raw in enumerate(iterator,1):
                count+=1
                key=f'{raw["document_id"]}:{raw["page"]}' if kind=='pages' else (f'{p.stem}:{i}' if not raw.get('id') else None)
                r=add(c,kind,raw,f'{p.relative_to(ROOT)}#row={i}',key)
                if kind=='documents':docmap[c,raw['id']]=r
                if kind=='pages':pagemap[c,str(raw['document_id']),int(raw['page'])]=r
            input_counts[str(p.relative_to(ROOT))]=count
        # Original company indices and manifests are independent searchable evidence.
        for p in sorted((ROOT/c).glob('*.csv')):
            count=0
            for i,raw in enumerate(csv_rows(p),1):
                add(c,'collections' if p.stem=='collection_index' else 'sources',raw,f'{p.relative_to(ROOT)}#row={i}',f'{p.stem}:{i}');count+=1
            input_counts[str(p.relative_to(ROOT))]=count
        for filename in ['evidence_manifest.json','download_manifest.json','unavailable_sources.json','historical_sources.json','research/web_sources.json','research/documents.json','research/product_images.json','research/products.json']:
            p=ROOT/c/filename
            if not p.exists():continue
            count=0
            for i,raw in enumerate(json_rows(p),1):
                if not isinstance(raw,dict):raw={'value':raw}
                if 'file' in raw:raw['local_file']=raw['file']
                kind='images' if filename.endswith('product_images.json') else 'sources'
                r=add(c,kind,raw,f'{c}/{filename}#record={i}',f'{filename}:{i}');count+=1
                # Corresponding raw product records retain fields absent from CSV.
                if filename.endswith('products.json'):
                    target=byid.get(f'{c}:products:{raw.get("id")}')
                    if target:edge(target,r,'Původní produktový záznam',proof=r['id'])
            input_counts[str(p.relative_to(ROOT))]=count
        # All original plain texts remain directly accessible, including orphan files.
        for p in sorted((ROOT/c/'raw').rglob('*.txt')):
            r=add(c,'sources',{'name':p.name,'local_file':str(p.relative_to(ROOT/c)),'text':p.read_text(errors='replace')},str(p.relative_to(ROOT)),str(p.relative_to(ROOT/c)))
            details[r['id']]['dates'].extend(dates(details[r['id']]['text'],str(p.relative_to(ROOT))))
            for ext in ['.pdf','.html']:
                for target in files.get(str(p.with_suffix(ext).relative_to(ROOT)),set()):edge(r,target,'Původní textová reprezentace',proof=str(p.relative_to(ROOT)))
        # Read all extraction metadata, all page text representations and exact page links.
        for p in sorted((ROOT/c/'research/extracted').glob('*.json')):
            ex=json.loads(p.read_text());doc=docmap.get((c,ex.get('document_id')))
            if not doc:raise ValueError(f'Unmapped extraction {p}')
            dd=details[doc['id']];dd['metadata']=ex.get('metadata',{})
            for key,meaning in [('creationDate','pdf_creation'),('modDate','pdf_revision')]:
                v=dd['metadata'].get(key,'');m=re.match(r'D:(\d{4})(\d{2})(\d{2})',v or '')
                if m:dd['dates'].append(dict(value='-'.join(m.groups()),precision='day',meaning=meaning,raw=v,source=str(p.relative_to(ROOT)),page=None))
            for pg in ex.get('pages',[]):
                page=pagemap.get((c,ex['document_id'],int(pg['page'])))
                if page:
                    t=pg.get('text','');details[page['id']]['text']=t
                    details[page['id']]['dates'].extend(dates(t,str(p.relative_to(ROOT)),pg['page']))
                    dd['dates'].extend(dates(t,str(p.relative_to(ROOT)),pg['page']))
        for p in sorted((ROOT/c/'research/pages').glob('*/text.jsonl')):
            doc=docmap.get((c,p.parent.name));n=0
            with p.open() as f:
                for line in f:
                    pg=json.loads(line);n+=1;page=pagemap.get((c,p.parent.name,int(pg['page'])))
                    if not page:raise ValueError(f'Unmapped page {p}:{pg["page"]}')
                    d=details[page['id']];t=pg.get('text','');d['text']=t;d['origins'].append(f'{p.relative_to(ROOT)}#line={n}')
                    d['dates'].extend(dates(t,str(p.relative_to(ROOT)),pg['page']))
                    page['search']+= ' '+norm(' '.join(dict.fromkeys(t.split())))
                    if doc:details[doc['id']]['dates'].extend(dates(t,str(p.relative_to(ROOT)),pg['page']))
            input_counts[str(p.relative_to(ROOT))]=n
    for parent in list(rows):
        raw=details[parent['id']]['raw']
        if parent['kind'] not in ['products','collections']:continue
        for field in ['gallery_urls','image_urls']:
            values=raw.get(field) or []
            if not isinstance(values,list):continue
            for u in values:
                if not isinstance(u,str) or not u.startswith(('http://','https://')):continue
                if not any(byid[i]['kind']=='images' for i in urlmap.get((parent['company'],u),[])):
                    add(parent['company'],'images',{'url':u,'name':parent['name']+' · galerie','scope_brand':parent['brand'],'status':'Galerie — URL zachována, originál nestažen'},details[parent['id']]['origins'][0],hashlib.sha256(u.encode()).hexdigest()[:24])
    print('Linking',len(rows),'records',flush=True)
    # Exact identity by URL or local file is evidence, not a name guess.
    for ids in list(urlmap.values())+list(files.values()):
        ids=sorted(ids)
        if len(ids)>1:
            anchor=min(ids,key=lambda i:({'documents':0,'products':1,'collections':2,'pages':3,'images':4}.get(byid[i]['kind'],5),i))
            for rid in ids:
                edge(anchor,rid,'Stejná zdrojová identita',proof=anchor)
    products_by_code=defaultdict(list)
    for r in rows:
        d=details[r['id']];raw=d['raw'];c=r['company'];kind=r['kind']
        if kind=='products' and r['code']:products_by_code[c,r['brand'],codekey(r['code'])].append(r)
        if kind in ['pages','codes']:
            doc=docmap.get((c,raw.get('document_id')));page=pagemap.get((c,raw.get('document_id'),int(raw.get('page') or 0)))
            if doc:
                r['catalogs']=[doc['id']];edge(r,doc,'Stránka / výskyt v dokumentu',proof=d['origins'][0]);r['brand']=doc['brand']
                if kind=='pages':r['name']=doc['name']+' · strana '+str(raw['page']);d['file']=details[doc['id']]['file']
            if kind=='codes':edge(r,page,'Artiklový výskyt na stránce',proof=d['origins'][0])
        if kind=='documents':
            r['catalogs']=[r['id']]
            page=pagemap.get((c,raw.get('id'),1))
            if page:r['image']=page['image'];d['preview']=details[page['id']]['preview']
            for y in raw.get('year_hints') or []:d['dates'].append(dict(value=str(y),precision='year',meaning='filename_hint',raw=str(y),source=d['url'],page=None))
        if kind=='images':
            for occurrence in raw.get('occurrences') or []:
                doc=docmap.get((c,occurrence['document_id']))
                if doc:
                    r['catalogs'].append(doc['id']);edge(r,doc,'Vložený obrázek v PDF',proof=d['origins'][0])
                    for num in occurrence.get('pages',[]):edge(r,pagemap.get((c,occurrence['document_id'],int(num))),'Obrázek na přesné stránce',proof=d['origins'][0]+' · xref a opakované výskyty v původních parametrech')
        for field in ['pdf_urls','document_url','listing_source','source_url','detail_url','source_index']:
            values=raw.get(field) or []
            if isinstance(values,str):values=values.split(' | ')
            if not isinstance(values,list):continue
            for u in values:
                if not isinstance(u,str):continue
                targets=urlmap.get((c,u),[])
                preferred={}
                for target in sorted(targets):preferred.setdefault(byid[target]['kind'],target)
                for target in preferred.values():
                    tr=byid[target]
                    if target==r['id']:continue
                    edge(r,tr,'Přímý zdrojový odkaz',proof=field+': '+u)
                    if tr['kind']=='documents' and field in ['pdf_urls','document_url']:r['catalogs'].append(target)
                    if tr['kind']=='collections' and field=='listing_source':r['collections'].append(target)
        # Gallery URLs are preserved as evidence; no remote images are fetched by the UI.
        for u in (raw.get('gallery_urls') or [])+(raw.get('image_urls') or []) if isinstance(raw.get('gallery_urls',[]),list) and isinstance(raw.get('image_urls',[]),list) else []:
            for target in urlmap.get((c,u),[]):edge(r,target,'Galerie — přesná URL',proof=u)
    for r in rows:
        if r['kind']=='codes':
            for p in products_by_code.get((r['company'],r['brand'],codekey(r['code'])),[]):edge(r,p,'Možná shoda artiklu v rámci firmy a značky','possible',details[r['id']]['origins'][0]+' · původní reference: '+str(r['code'])+' / '+str(p['code']))
    # Collection names alone are possible matches, never a confirmed catalogue membership.
    collection_names=defaultdict(list)
    for r in rows:
        if r['kind']=='collections':collection_names[r['company'],r['brand'],norm(r['name'])].append(r)
    for r in rows:
        if r['kind']=='products':
            props=details[r['id']]['properties']
            for k in ['Collection','Kollektion','collection','Kolekce']:
                value=props.get(k) if isinstance(props,dict) else None
                if isinstance(value,str):
                    for target in collection_names.get((r['company'],r['brand'],norm(value)),[]):edge(r,target,'Možná shoda názvu kolekce','possible',f'{k}: {value}')
    # Propagate only documented direct collection membership, not graph connectivity.
    for r in rows:
        for cid in r['collections']:r['catalogs'].extend(byid[cid]['catalogs'])
        r['catalogs']=sorted(set(r['catalogs']))
    for r in rows:
        if r['kind']=='images':
            for rel in details[r['id']]['relations']:
                parent=byid[rel['id']]
                if rel['label']=='Galerie — přesná URL' and parent['kind'] in ['products','collections']:
                    r['catalogs'].extend(parent['catalogs'])
            r['catalogs']=sorted(set(r['catalogs']))
            if r['catalogs']:r['brand']=' / '.join(sorted({byid[c]['brand'] for c in r['catalogs']}))
    populate_collection_filters(rows)
    usable={'publication','revision','document_date','publisher_label','article_date'}
    for r in rows:
        d=details[r['id']]
        # Include dates in source representations of the same exact PDF identity.
        if r['kind']=='documents':
            for rel in d['relations']:
                if rel['label'] in ['Původní textová reprezentace','Stejná zdrojová identita']:
                    d['dates'].extend(x for x in details[rel['id']]['dates'] if x['meaning'] in usable)
        d['dates']=list({json.dumps(v,sort_keys=True,ensure_ascii=False):v for v in d['dates']}.values())
        r['dateValues']=sorted({x['value'] for x in d['dates'] if x['meaning'] in usable})
    for r in rows:
        d=details[r['id']]
        if r['kind'] in ['products','pages','images','codes','collections']:
            r['dateValues']=sorted(set(r['dateValues']+[v for cid in r['catalogs'] for v in byid[cid]['dateValues']]))
        r['years']=sorted({int(v[:4]) for v in r['dateValues']})
        if r['kind']=='products':r['search']+=' '+norm(' '.join(byid[i]['name'] for i in r['catalogs']+r['collections']))
        if r['evidence']!='problem':r['evidence']='confirmed' if any(e['certainty']=='confirmed' for e in d['relations']) else 'possible' if d['relations'] else 'standalone'
        if not d['relations']:d['unresolved_reason']='Žádná přesná vazba nalezena; záznam zůstává samostatně dohledatelný.'
        d['date_conflict']=len({x['value'] for x in d['dates'] if x['meaning'] in ['publication','document_date','publisher_label']})>1
        # Do not silently drop file references: report each missing local target.
        references={r['image'],d['file'],d['preview']}
        def local_references(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in ['local_file','detail_local_file','mask_local_file','source_local_file','source_pdf','thumb','preview'] and isinstance(item,str) and item and not item.startswith(('http:','https:')):references.add(local(r['company'],item))
                    elif isinstance(item,(dict,list)):local_references(item)
            elif isinstance(value,list):
                for item in value:local_references(item)
        local_references(d['raw'])
        d['local_references']=sorted(p for p in references if p)
        d['missing_files']=[p for p in d['local_references'] if not (ROOT/p).is_file()]
        if d['missing_files']:d['unresolved_reason']='Chybí místní soubor: '+', '.join(d['missing_files'])
    print('Writing shards',flush=True)
    for r in rows:
        details[r['id']]['relations'].sort(key=lambda e:(e['certainty'],e['id'],e['label'],e['proof']))
        for rel in details[r['id']]['relations']:
            target=byid[rel['id']]
            rel['target']={k:target[k] for k in ['id','name','kind']}
    # All executable data uses JSON serialization, never source HTML or scripts.
    def jswrite(path,expression,value):
        path.write_text(expression+'='+json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('\u2028','\\u2028').replace('\u2029','\\u2029')+';\n')

    # Bounded chunks allow the frontend to fetch only a selected detail.
    chunk_index=0;group={};group_rows=[];size=0
    def flush():
        nonlocal chunk_index,group,group_rows,size
        if not group:return
        chunk=str(chunk_index)
        jswrite(OUT/'details'/f'{chunk}.js',f'window.AmonArchive.details[{json.dumps(chunk)}]',group)
        for row in group_rows:row['chunk']=chunk
        chunk_index+=1;group={};group_rows=[];size=0
    for r in rows:
        record=details[r['id']];weight=len(json.dumps(record,ensure_ascii=False))
        if group and (size+weight>1_000_000 or len(group)>=200):flush()
        group[r['id']]=record;group_rows.append(r);size+=weight
    flush()
    for stale in (OUT/'details').glob('*.js'):
        if int(stale.stem)>=chunk_index:stale.unlink()
    for stale in (OUT/'details').glob('*.json'):stale.unlink()  # obsolete generated format only
    for c in COMPANIES:
        subset=[r for r in rows if r['company']==c]
        audit[c]=dict(kinds=dict(Counter(r['kind'] for r in subset)),states=dict(Counter(r['evidence'] for r in subset)),dated_documents=sum(bool(r['dateValues']) for r in subset if r['kind']=='documents'),date_conflicts=sum(details[r['id']]['date_conflict'] for r in subset),missing_file_records=sum(bool(details[r['id']]['missing_files']) for r in subset))
    audit['inputs']=input_counts;audit['totals']=dict(Counter(r['kind'] for r in rows));audit['relationships']=len(edges)
    audit['text_files']=sum(1 for c in COMPANIES for p in (ROOT/c/'raw').rglob('*.txt'))
    audit['embedded_images']=sum(input_counts.get(c+'/research/exports/embedded_images.csv',0) for c in COMPANIES)
    audit['date_semantics']='Publication, revision and documentary imprints are filterable; creation, archival capture, download and filename hints remain separate evidence. Date conflicts are multiple imprints, not automatically errors.'
    audit['streaming_quarantine']='Oversized string fields above 256 KiB are excluded from display, recorded as unresolved, and retained in the untouched original at the recorded source locator.'
    (OUT/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2))
    payload=dict(companies=COMPANIES,counts={c:sum(r['kind']=='products' and r['company']==c for r in rows) for c in COMPANIES},catalogs=[dict(id=r['id'],company=r['company'],name=r['name']) for r in rows if r['kind']=='documents'],collections=[dict(id=r['id'],company=r['company'],name=r['name']) for r in rows if r['kind']=='collections'],rows=rows)
    (OUT/'index.json').write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')))
    meta={k:v for k,v in payload.items() if k!='rows'}
    meta['total']=len(rows)
    jswrite(OUT/'data.js','window.AmonArchive',dict(meta=meta,index={},search={},details={}))
    (OUT/'indices').mkdir(exist_ok=True)
    for kind in sorted({r['kind'] for r in rows}):
        group=[r for r in rows if r['kind']==kind]
        jswrite(OUT/'indices'/f'{kind}.js',f'window.AmonArchive.index[{json.dumps(kind)}]',[{k:v for k,v in r.items() if k!='search'} for r in group])
        jswrite(OUT/'indices'/f'{kind}-search.js',f'window.AmonArchive.search[{json.dumps(kind)}]',{r['id']:r['search'] for r in group})
    print(json.dumps(audit['totals']),flush=True)

if __name__=='__main__':build()
