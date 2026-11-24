"""Extract second-batch references with honest PDF/HTML/JATS locators."""
import hashlib
import json
from pathlib import Path
import pypdf
from bs4 import BeautifulSoup
from pcb_assistant.training.common import dump_json
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'data/public_lamination_v2'

def extract():
 manifest=json.loads((OUT/'manifest.json').read_text());units=[]
 for m in manifest:
  if m['status']!='downloaded':continue
  path=OUT/m['raw_path'];assert hashlib.sha256(path.read_bytes()).hexdigest()==m['sha256']
  parts=[]
  if path.suffix=='.pdf':
   reader=pypdf.PdfReader(str(path))
   m['pdf_metadata']=dict(reader.metadata or {})
   parts=[('pdf_page',str(i+1),page.extract_text() or '') for i,page in enumerate(reader.pages)]
  elif path.suffix=='.xml':
   soup=BeautifulSoup(path.read_bytes(),'xml')
   if not soup.find('body'):raise ValueError('article_has_no_body:'+m['id'])
   m['verified_article_title']=soup.find('article-title').get_text(' ',strip=True)
   if soup.find('license'):
    m['license_statement']=soup.find('license').get_text(' ',strip=True)
    m['rights_status']='see_original_article_license'
   m['authors']=[c.get_text(' ',strip=True) for c in soup.select('contrib[contrib-type="author"] name')]
   m['publication_dates']=[p.get_text('-',strip=True) for p in soup.find_all('pub-date')]
   if soup.find('abstract'):parts.append(('article_abstract','Abstract',soup.find('abstract').get_text(' ',strip=True)))
   for sec in soup.find('body').find_all('sec'):
    title=sec.find('title',recursive=False)
    title=title.get_text(' ',strip=True) if title else sec.get('id','untitled section')
    own=[c.get_text(' ',strip=True) for c in sec.find_all(recursive=False) if c.name not in ['sec','title']]
    text='\n'.join(own)
    if text.strip():parts.append(('article_section',title,text))
   m['extraction_limit']='JATS sections preserved; equation structure and figures require original XML/figure inspection, no graphic interpretation claimed.'
  else:
   soup=BeautifulSoup(path.read_bytes(),'html.parser')
   for e in soup(['script','style','nav','footer','header']):e.decompose()
   parts=[('html_document',soup.title.get_text(' ',strip=True) if soup.title else m['title'],soup.get_text('\n',strip=True))]
  for i,(kind,locator,text) in enumerate(parts,1):
   units.append(dict(source_id=m['id'],source_group=m['source_group'],split=m['split'],unit=i,locator_type=kind,locator=locator,text=text,sha256=hashlib.sha256(text.encode()).hexdigest()))
  m.update(units=len(parts),text_characters=sum(len(p[2]) for p in parts),content_status='usable_text' if any(len(p[2])>100 for p in parts) else 'empty',revision_status='archived edition; not asserted latest or universally applicable')
  (OUT/'text'/(m['id']+'.txt')).write_text('\n\n'.join(f'=== UNIT {i}: {kind} {loc} ===\n{text}' for i,(kind,loc,text) in enumerate(parts,1)))
 dump_json(OUT/'manifest.json',manifest)
 (OUT/'units.jsonl').write_text(''.join(json.dumps(u,ensure_ascii=False)+'\n' for u in units))
 print(json.dumps({'documents':len(manifest),'usable':sum(m.get('content_status')=='usable_text' for m in manifest),'units':len(units)}))
if __name__=='__main__':extract()
