#!/usr/bin/env python3
"""Package the existing board for GitHub Pages, retaining every data record.
The local archive stays untouched. Pages serves optimized previews; original
PDFs and full-size assets use their recorded publisher URL when available.
"""
import concurrent.futures
import gzip
import hashlib
import json
import shutil
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parent.parent
DEST=ROOT/'pages-deploy'
SITE=DEST/'site'
SITE.mkdir(parents=True,exist_ok=True)

def dump_gz(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('wb') as f:
        with gzip.GzipFile(fileobj=f,mode='wb',mtime=0,compresslevel=6) as g:g.write(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())

def readjs(path):return json.loads(path.read_text().split('=',1)[1].strip().removesuffix(';'))
index=json.loads((ROOT/'board/index.json').read_text())
byid={r['id']:r for r in index['rows']}
doc_urls={};doc_chunks={}
for row in index['rows']:
    if row['kind']=='documents':
        if row['chunk'] not in doc_chunks:doc_chunks[row['chunk']]=readjs(ROOT/'board/details'/(row['chunk']+'.js'))
        doc_urls[row['id']]=doc_chunks[row['chunk']][row['id']]['url']
del doc_chunks
assets={r['image'] for r in index['rows'] if r['image'] and (ROOT/r['image']).is_file()}
assetmap={p:'media/'+hashlib.sha256(p.encode()).hexdigest()[:24]+'.webp' for p in assets}
def convert(item):
    original,target=item;out=SITE/target;out.parent.mkdir(parents=True,exist_ok=True)
    if not out.exists():
        with Image.open(ROOT/original) as im:
            im.thumbnail((640,640));im.convert('RGB').save(out,'WEBP',quality=75,method=3)
    return out.stat().st_size
print('Optimizing',len(assets),'local previews',flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    sizes=list(pool.map(convert,assetmap.items()))
print('Previews MB',sum(sizes)/1e6,flush=True)
# Sources remain traceable without generating broken local-only links.
for p in sorted((ROOT/'board/details').glob('*.js')):
    value=readjs(p)
    for rid,d in value.items():
        if not d.get('url') and byid[rid]['catalogs']:
            d['url']=doc_urls.get(byid[rid]['catalogs'][0],'')
            if d['url'] and d.get('page'):d['url']+='#page='+str(d['page'])
        d['archive_file']=d.get('file','');d['file']='';d['preview']=''
    dump_gz(SITE/'board/details'/(p.name+'.json.gz'),value)
for p in (ROOT/'board/indices').glob('*.js'):
    value=readjs(p)
    if isinstance(value,list):
        for r in value:r['image']=assetmap.get(r['image'],r['image'] if r['image'].startswith('https://') else '')
    dump_gz(SITE/'board/indices'/(p.name+'.json.gz'),value)
for name in ['data.js','filter.js','style.css','audit.html','audit.json','verification.json']:
    shutil.copyfile(ROOT/'board'/name,SITE/'board'/name)
app=(ROOT/'board/app.js').read_text()
a=app.index('function loadScript(path)');b=app.index('async function ensureKind',a)
app=app[:a]+'''function loadScript(path){if(!loads.has(path))loads.set(path,(async()=>{const response=await fetch(path+'.json.gz');if(!response.ok)throw Error('Data nejsou dostupná: '+response.status);const value=await new Response(response.body.pipeThrough(new DecompressionStream('gzip'))).json();const name=path.split('/').pop().replace(/\\.js$/,'');if(path.includes('/details/'))archive.details[name]=value;else if(name.endsWith('-search'))archive.search[name.slice(0,-7)]=value;else archive.index[name]=value;})().catch(e=>{loads.delete(path);throw e;}));return loads.get(path);}
''' + app[b:]
# Only URLs and files actually included in the deployed site become links.
app=app.replace("if(!url||/^(?!https?:)","if(url&&!/^https?:/i.test(url)&&!url.startsWith('board/')&&!url.startsWith('media/'))return el('span',title+' · místní archiv');if(!url||/^(?!https?:)")
app=app.replace("if(d.unresolved_reason)body.append", "if(d.archive_file)body.append(el('p','Originál je zachován v místním archivu; online je dostupný odkaz na vydavatele.'));if(d.unresolved_reason)body.append")
(SITE/'board/app.js').write_text(app)
page=(ROOT/'PRODUCT_BOARD.html').read_text()
start=page.index('<p>Původní archivy:');end=page.index('</p>',start)+4
page=page[:start]+'<p>Online vydání zobrazuje celý datový index a optimalizované místní náhledy. Originální soubory zůstávají v místním archivu; jejich veřejné zdroje jsou uvedeny v detailech.</p>'+page[end:]
(SITE/'index.html').write_text(page)
(SITE/'PRODUCT_BOARD.html').write_text(page)
(SITE/'.nojekyll').touch()
size=sum(p.stat().st_size for p in SITE.rglob('*') if p.is_file())
assert size<950_000_000,('Pages package too large',size)
(DEST/'package-report.json').write_text(json.dumps({'records':len(index['rows']),'previews':len(assets),'bytes':size,'original_archive_unchanged':True,'original_files_hosted':False},indent=2))
workflow=DEST/'.github/workflows/pages.yml';workflow.parent.mkdir(parents=True,exist_ok=True)
workflow.write_text('''name: Deploy AMON board to Pages
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: pages
  cancel-in-progress: true
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - name: Deploy
        id: deployment
        uses: actions/deploy-pages@v4
''')
(DEST/'README.md').write_text('# AMON product board\n\nStatic archive browser. Published data includes every board record and optimized previews. Full original documents remain in the local research archive; recorded publisher URLs are linked from details. No live website collection or OCR is performed.\n\nDeploy `site/` using the included GitHub Pages workflow.\n')
print('READY',size,'bytes',flush=True)
