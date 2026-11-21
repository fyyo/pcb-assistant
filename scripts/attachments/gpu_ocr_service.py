"""One local GPU OCR engine shared by CPU document workers via private temp files."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def libraries():
    if os.environ.get('PCB_OCR_LIBRARIES_READY') == '1':
        return
    paths = []
    for environment in ('.venv-ocr', '.venv-train'):
        paths.extend(str(path) for path in (ROOT / environment / 'lib/python3.12/site-packages/nvidia').glob('*/lib'))
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = ':'.join(paths)
    env['PCB_OCR_LIBRARIES_READY'] = '1'
    env['OMP_NUM_THREADS'] = '2'
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env)


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    libraries()
    import onnxruntime
    onnxruntime.disable_telemetry_events()
    from rapidocr_onnxruntime import RapidOCR
    from rapidocr_onnxruntime.utils.infer_engine import OrtInferSession
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--diagnostic-dir', type=Path)
    args = parser.parse_args()
    original_init = OrtInferSession.__init__
    def initialize(self, configuration):
        name = str(configuration.get('model_path', ''))
        self._memory_limit = (1536 if 'det_' in name else 768 if 'rec_' in name else 128) * 1024 * 1024
        original_init(self, configuration)
    OrtInferSession.__init__ = initialize
    def providers(self):
        if not self.cfg_use_cuda:
            self.use_cuda, self.use_directml = False, False
            return ['CPUExecutionProvider']
        self.use_cuda, self.use_directml = True, False
        return [('CUDAExecutionProvider', {'device_id': 0, 'arena_extend_strategy': 'kSameAsRequested',
                 'gpu_mem_limit': self._memory_limit, 'cudnn_conv_algo_search': 'HEURISTIC',
                 'cudnn_conv_use_max_workspace': '0', 'do_copy_in_default_stream': True}), 'CPUExecutionProvider']
    OrtInferSession._get_ep_list = providers
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                      det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
    sessions = [engine.text_det.infer.session, engine.text_cls.infer.session, engine.text_rec.session.session]
    if not all(session.get_providers()[0] == 'CUDAExecutionProvider' for session in sessions):
        raise RuntimeError('gpu_ocr_provider_unavailable')
    cpu_engine = None
    fallbacks = 0
    def recognize(image):
        nonlocal cpu_engine, fallbacks
        array = np.ascontiguousarray(np.asarray(image.convert('RGB'))[:, :, ::-1])
        try:
            result, _ = engine(array)
            return result, 'cuda'
        except Exception:
            fallbacks += 1
            if cpu_engine is None:
                cpu_engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                      det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False)
            result, _ = cpu_engine(array)
            return result, 'cpu_fallback'
    synthetic = Image.new('RGB', (1200, 1500), 'white')
    draw = ImageDraw.Draw(synthetic)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 22)
    for index in range(35):
        draw.text((20, 20 + index * 40), 'PCB temperature 180 C pressure 25 bar vacuum -0.095 MPa', fill='black', font=font)
    started = time.monotonic()
    result, backend = recognize(synthetic)
    if not result or not any('180' in row[1] for row in result):
        raise ValueError('gpu_ocr_synthetic_check_failed')
    print(json.dumps({'event': 'gpu_ocr_verified', 'seconds': round(time.monotonic() - started, 3),
                      'lines': len(result), 'backend': backend, 'providers': [session.get_providers()[0] for session in sessions]}), flush=True)
    for width, height in ((772, 209), (1398, 142), (2000, 2000)):
        sample = Image.new('RGB', (width, height), 'white')
        ImageDraw.Draw(sample).text((12, 20), 'PCB pressure 25 bar temperature 180 C', fill='black', font=font)
        result, backend = recognize(sample)
        if not result or not any('25' in row[1] for row in result):
            raise ValueError('aspect_ratio_check_failed')
        print(json.dumps({'event': 'gpu_ocr_shape_checked', 'size': [width, height], 'backend': backend}), flush=True)
    if args.diagnostic_dir:
        for sample in sorted(args.diagnostic_dir.glob('*.png'))[:20]:
            with Image.open(sample) as image:
                result, backend = recognize(image)
            print(json.dumps({'event': 'local_image_diagnostic', 'sample': sample.name, 'backend': backend,
                              'recognized_lines': len(result or [])}), flush=True)
    if args.check:
        return
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='pcb-gpu-ocr-', dir='/tmp') as folder:
        queue = Path(folder)
        queue.chmod(0o700)
        config = ROOT / '.private/gpu_ocr_service.json'
        save(queue / 'heartbeat.json', {'status': 'ready'})
        save(config, {'queue': str(queue), 'pid': os.getpid(), 'mode': 'local_files_only'})
        print(json.dumps({'event': 'gpu_ocr_service_ready'}), flush=True)
        processed, last_heartbeat = 0, 0
        try:
            while not (queue / 'STOP').exists():
                if time.monotonic() - last_heartbeat > 5:
                    save(queue / 'heartbeat.json', {'status': 'ready', 'processed': processed, 'cpu_fallbacks': fallbacks})
                    last_heartbeat = time.monotonic()
                requests = sorted(queue.glob('*.request.json'))
                if not requests:
                    time.sleep(.05)
                    continue
                for request in requests:
                    ident = request.name.split('.')[0]
                    if len(ident) != 32 or any(character not in '0123456789abcdef' for character in ident):
                        continue
                    raw = queue / (ident + '.png')
                    try:
                        with Image.open(raw) as image:
                            if image.width * image.height > 20000000:
                                raise ValueError('image_too_large')
                            result, backend = recognize(image)
                        normalized = [[np.asarray(row[0]).astype(float).tolist(), str(row[1]), float(row[2])] for row in result or []]
                        save(queue / (ident + '.response.json'), {'status': 'ok', 'result': normalized, 'backend': backend})
                    except Exception as exc:
                        save(queue / (ident + '.response.json'), {'status': 'failed', 'error_type': type(exc).__name__})
                    request.unlink(missing_ok=True)
                    raw.unlink(missing_ok=True)
                    processed += 1
                    save(queue / 'heartbeat.json', {'status': 'ready', 'processed': processed, 'cpu_fallbacks': fallbacks})
                    last_heartbeat = time.monotonic()
        finally:
            config.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(json.dumps({'event': 'gpu_ocr_failed', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
