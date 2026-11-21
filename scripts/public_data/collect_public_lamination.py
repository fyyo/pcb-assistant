"""Fetch only the reviewed public URL manifest; no private input or uploads."""
import concurrent.futures
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests

ROOT = Path(__file__).resolve().parents[2] / 'data/public_lamination_v1'
ALLOWED = {'www.isola-group.com','www.rogerscorp.com','www.ipc.org','www.electronics.org','www.itl.nist.gov','www.nist.gov','www.burkle.tech','lauffer.de','www.lauffer.de'}
ALLOWED.update({'ocw.mit.edu','www.ebi.ac.uk'})
LIMIT = 30 * 1024 * 1024
CACHED = {r['id']: r for r in json.loads((ROOT/'manifest.json').read_text())} if (ROOT/'manifest.json').exists() else {}

def fetch(spec):
    old = CACHED.get(spec['id'])
    if old and old.get('status') == 'downloaded' and all(old.get(k)==v for k,v in spec.items()):
        p=ROOT/old['raw_path']
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==old['sha256']:
            return old
    row = dict(spec)
    url = row['url']
    try:
        for _ in range(6):
            parsed=urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in ALLOWED:
                raise ValueError('non_allowlisted_public_url')
            response=requests.get(url, timeout=(15,45), stream=True, allow_redirects=False,
                                  headers={'User-Agent':'PCB-local-reference-study/1.0'})
            if response.is_redirect:
                url=urljoin(url,response.headers['Location']); response.close(); continue
            response.raise_for_status()
            body=bytearray()
            for block in response.iter_content(65536):
                body.extend(block)
                if len(body)>LIMIT:raise ValueError('document_size_limit')
            break
        else:raise ValueError('redirect_limit')
        pdf=body.startswith(b'%PDF-')
        if '.pdf' in urlparse(row['url']).path.lower() and not pdf:raise ValueError('not_a_pdf')
        suffix = '.pdf' if pdf else ('.xml' if url.endswith('/fullTextXML') else '.html')
        path=ROOT/'raw'/(row['id']+suffix)
        path.write_bytes(body)
        row.update(status='downloaded', final_url=url, fetched_at=datetime.now(timezone.utc).isoformat(),
                   http_status=response.status_code,content_type=response.headers.get('Content-Type'),
                   last_modified=response.headers.get('Last-Modified'),bytes=len(body),
                   sha256=hashlib.sha256(body).hexdigest(),raw_path=str(path.relative_to(ROOT)),
                   rights_status='publicly_accessible_copyright_not_transferred',
                   raw_text_training_allowed=False, usage='local_reference_and_original_fact_based_exercises')
        response.close()
    except Exception as exc:
        row.update(status='failed',error_type=type(exc).__name__)
    return row

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run', default='public_lamination_v1')
    args=parser.parse_args()
    if not args.run.startswith('public_lamination_') or not args.run.replace('_','').isalnum():
        raise ValueError('invalid_public_run')
    ROOT=Path(__file__).resolve().parents[2]/'data'/args.run
    CACHED={r['id']:r for r in json.loads((ROOT/'manifest.json').read_text())} if (ROOT/'manifest.json').exists() else {}
    seeds=json.loads((ROOT/'sources_seed.json').read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(fetch,seeds))
    (ROOT/'manifest.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'downloaded':sum(r['status']=='downloaded' for r in rows),'failed':[r['id'] for r in rows if r['status']=='failed']}))
