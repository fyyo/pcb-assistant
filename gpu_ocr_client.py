"""Private local-file IPC for OCR; no network connection or document upload."""
import io
import json
from pathlib import Path
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / '.private/gpu_ocr_service.json'


def recognize(image):
    if not CONFIG.exists():
        return None
    configuration = json.loads(CONFIG.read_text())
    queue = Path(configuration['queue']).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if not queue.is_dir() or queue.parent != temporary_root or not queue.name.startswith('pcb-gpu-ocr-'):
        return None
    heartbeat = queue / 'heartbeat.json'
    if not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 60:
        return None
    ident = uuid.uuid4().hex
    raw = queue / (ident + '.png')
    response = queue / (ident + '.response.json')
    request = queue / (ident + '.request.json')
    temporary = queue / (ident + '.tmp')
    stream = io.BytesIO()
    image.convert('RGB').save(stream, format='PNG')
    raw.write_bytes(stream.getvalue())
    raw.chmod(0o600)
    temporary.write_text(json.dumps({'id': ident}))
    temporary.chmod(0o600)
    temporary.replace(request)
    try:
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if response.exists():
                data = json.loads(response.read_text())
                if data.get('status') != 'ok':
                    raise RuntimeError('local_gpu_ocr_failed')
                return data['result']
            time.sleep(.05)
        raise TimeoutError('local_gpu_ocr_timeout')
    finally:
        for path in (raw, response, request, temporary):
            path.unlink(missing_ok=True)
