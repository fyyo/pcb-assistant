"""Download public, pinned model artifacts only; never read or upload mail data."""
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def main():
    spec = json.loads((ROOT / 'base_model.json').read_text())
    target = ROOT / 'models' / spec['model_id'].split('/')[-1]
    target.mkdir(parents=True, exist_ok=True)
    for name, (expected_size, expected_hash) in spec['files'].items():
        if Path(name).name != name:
            raise ValueError('invalid_artifact_path')
        destination = target / name
        if destination.exists() and destination.stat().st_size == expected_size and digest(destination) == expected_hash:
            continue
        partial = target / (name + '.partial')
        query = urllib.parse.urlencode({'Revision': spec['revision'], 'FilePath': name})
        url = 'https://modelscope.cn/api/v1/models/' + spec['model_id'] + '/repo?' + query
        for attempt in range(6):
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                if offset >= expected_size:
                    if offset == expected_size and digest(partial) == expected_hash:
                        partial.replace(destination)
                        break
                    offset = 0
                headers = {'User-Agent': 'pcb-local-model-setup/1.0'}
                if offset:
                    headers['Range'] = f'bytes={offset}-'
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=45) as response:
                    if offset and response.status != 206:
                        offset = 0
                    if offset and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        raise ValueError('invalid_resume_response')
                    done = offset
                    last = time.monotonic()
                    with partial.open('ab' if offset else 'wb') as stream:
                        while block := response.read(2 * 1024 * 1024):
                            stream.write(block)
                            done += len(block)
                            if done > expected_size:
                                raise ValueError('artifact_size_exceeded')
                            if time.monotonic() - last >= 20:
                                print(json.dumps({'event': 'public_model_download', 'file': name,
                                                  'received_mib': round(done / 2**20),
                                                  'total_mib': round(expected_size / 2**20)}), flush=True)
                                last = time.monotonic()
                if partial.stat().st_size != expected_size or digest(partial) != expected_hash:
                    raise ValueError('artifact_integrity_failure')
                partial.replace(destination)
                print(json.dumps({'event': 'public_model_file_verified', 'file': name}), flush=True)
                break
            except Exception as exc:
                print(json.dumps({'event': 'public_model_download_retry', 'file': name,
                                  'attempt': attempt + 1, 'error_type': type(exc).__name__}), flush=True)
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 8))
    (target / 'download_manifest.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'event': 'public_base_model_ready', 'files': len(spec['files'])}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'event': 'public_model_download_failed', 'error_type': type(exc).__name__}))
        sys.exit(1)
