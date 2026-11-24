"""Page-addressable local reference extraction; no automatic training intake."""
import json
import hashlib
from pathlib import Path
import pypdf
from bs4 import BeautifulSoup
ROOT=Path(__file__).resolve().parents[2]/'data/public_lamination_v1'

def extract():
 rows=json.loads((ROOT/'manifest.json').read_text()); pages=[]
 for r in rows:
  if r['status']!='downloaded':continue
  path=ROOT/r['raw_path']
  assert hashlib.sha256(path.read_bytes()).hexdigest()==r['sha256']
  if path.suffix=='.pdf':
   reader=pypdf.PdfReader(str(path))
   r['pdf_metadata']=dict(reader.metadata or {})
   texts=[page.extract_text() or '' for page in reader.pages]
  else:
   soup=BeautifulSoup(path.read_bytes(),'html.parser')
   for e in soup(['script','style','nav','header','footer']):e.decompose()
   texts=[soup.get_text('\n',strip=True)]
  r['pages']=len(texts);r['extracted_characters']=sum(map(len,texts))
  r['content_status']='usable_text' if r['extracted_characters']>=100 else 'unusable_empty_document'
  r['revision_status']='Retrieved edition only; not certified as latest applicable specification'
  r['document_revision']={
   'isola_408_guide':'Last Updated March 24, 2026',
   'rogers_4400_guide':'Revised 1628 010323; Publication #92-005; copyright 2023',
   'lauffer_press':'L002 - Stand 09/2011',
   'ipc_resin':'2.3.16 B, 12/94','ipc_flow':'2.3.17 D, 8/97',
   'ipc_gel':'2.3.18 A, 4/86','ipc_volatiles':'2.3.19 C, 12/94',
   'ipc_peel':'2.4.8 C, 12/94','ipc_bow':'2.4.22 C, 6/99',
   'ipc_tma':'2.4.24 C, 12/94','ipc_delamination':'2.4.24.1, 12/94',
   'ipc_dsc':'2.4.25 D, 11/17',
  }.get(r['id'],'No explicit revision transcribed; see archived original and PDF metadata. PDF creation date is not publication date.')
  r['extraction']='pypdf_text_no_ocr' if path.suffix=='.pdf' else 'beautifulsoup_text'
  r['empty_pages']=[i+1 for i,t in enumerate(texts) if len(t.strip())<60]
  (ROOT/'text'/(r['id']+'.txt')).write_text('\n\n'.join(f'=== PAGE {i+1} ===\n{t}' for i,t in enumerate(texts)))
  for i,t in enumerate(texts):
   pages.append({'source_id':r['id'],'source_group':r['source_group'],'split':r['split'],'page':i+1,'text':t,'sha256':hashlib.sha256(t.encode()).hexdigest()})
 (ROOT/'pages.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in pages))
 (ROOT/'manifest.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'documents':len(rows),'pages':len(pages),'empty_pages':{r['id']:r['empty_pages'] for r in rows if r.get('empty_pages')}}))
if __name__=='__main__':extract()
