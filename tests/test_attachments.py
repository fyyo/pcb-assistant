"""Synthetic-only attachment extraction and parent grouping tests."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import threading
import time
from unittest.mock import patch
import zipfile

from attachment_extract import extract_attachment, file_kind
from mail_privacy import IdentityIndex, Sanitizer
from combine_training_sources import filter_ocr_text
from sanitize_attachments import cache_reusable


class AttachmentTests(unittest.TestCase):
    def test_cache_migration_retries_limited_and_failed_documents(self):
        cached = {'processing_signature': 'old', 'status': 'processed', 'parser_stats': {}}
        self.assertTrue(cache_reusable(cached, 'new', 'old'))
        self.assertFalse(cache_reusable(cached, 'new'))
        self.assertFalse(cache_reusable(cached | {'status': 'parse_failed'}, 'new', 'old'))
        self.assertFalse(cache_reusable(cached | {'parser_stats': {'page_limit_reached': 1}}, 'new', 'old'))
        self.assertFalse(cache_reusable(cached | {'parser_stats': {'table_empty_tail_stopped': 1}}, 'new', 'old'))
        self.assertTrue(cache_reusable(cached | {'parser_stats': {'page_limit_reached': 0}}, 'new', 'old'))

    def test_sparse_spreadsheet_does_not_stop_at_blank_rows(self):
        from openpyxl import Workbook
        workbook = Workbook(); sheet = workbook.active
        sheet.cell(1, 1, '压合试验记录')
        sheet.cell(500, 1, '压力25bar，温度180°C')
        stream = io.BytesIO(); workbook.save(stream)
        units, stats = extract_attachment(stream.getvalue(), 'xlsx', ocr=False)
        self.assertIn('行500', '\n'.join(unit.text for unit in units))
        self.assertNotIn('table_empty_tail_stopped', stats)

    def test_private_gpu_ipc_cleans_temporary_payloads(self):
        from PIL import Image
        import gpu_ocr_client
        with tempfile.TemporaryDirectory(prefix='pcb-gpu-ocr-') as folder:
            queue = Path(folder)
            config = queue / 'config.json'
            config.write_text(json.dumps({'queue': str(queue)}))
            (queue / 'heartbeat.json').write_text('{}')
            def fake_service():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    requests = list(queue.glob('*.request.json'))
                    if requests:
                        request = requests[0]
                        ident = request.name.split('.')[0]
                        (queue / (ident + '.response.json')).write_text(json.dumps({'status': 'ok', 'result': [[[0, 0], 'synthetic 25 bar', .99]]}))
                        return
                    time.sleep(.01)
            service = threading.Thread(target=fake_service)
            service.start()
            with patch.object(gpu_ocr_client, 'CONFIG', config):
                result = gpu_ocr_client.recognize(Image.new('RGB', (300, 100), 'white'))
            service.join()
            self.assertEqual(result[0][1], 'synthetic 25 bar')
            self.assertFalse(list(queue.glob('*.png')))
            self.assertFalse(list(queue.glob('*.request.json')))
            self.assertFalse(list(queue.glob('*.response.json')))

    def test_spreadsheet_rows_units_and_no_formula_execution(self):
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Synthetic Private Sheet Name'
        sheet.append(['压合温度 °C', '压力 bar', '保温 min'])
        sheet.append([180, 25, 60])
        sheet.append(['=1+1', None, None])
        stream = io.BytesIO()
        workbook.save(stream)
        units, stats = extract_attachment(stream.getvalue(), 'xlsx', ocr=False)
        text = '\n'.join(unit.text for unit in units)
        self.assertIn('列1=180', text)
        self.assertIn('列2=25', text)
        self.assertIn('列3=60', text)
        self.assertIn('°C', text)
        self.assertNotIn('=1+1', text)
        self.assertNotIn(sheet.title, text)
        self.assertEqual(stats['formula_cache_only'], 1)

    def test_docx_table_and_embedded_image_policy(self):
        from docx import Document
        document = Document()
        document.add_paragraph('压合出现白斑，建议检查真空条件。')
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = '压力 bar'
        table.cell(1, 0).text = '25'
        table.cell(0, 1).text = '温度 °C'
        table.cell(1, 1).text = '180'
        stream = io.BytesIO()
        document.save(stream)
        self.assertEqual(file_kind('incorrect.bin', stream.getvalue()), 'docx')
        units, _ = extract_attachment(stream.getvalue(), 'docx', ocr=False)
        text = '\n'.join(unit.text for unit in units)
        self.assertIn('检查真空', text)
        self.assertIn('列1=25', text)
        self.assertIn('列2=180', text)

    def test_pdf_pages_and_ocr_chinese_parameters(self):
        import pymupdf
        from PIL import Image
        document = pymupdf.open()
        page = document.new_page(width=700, height=230)
        page.insert_text((25, 80), '压合温度180℃ 压力25bar 保温60min', fontname='china-s', fontsize=26)
        content = document.tobytes()
        units, _ = extract_attachment(content, 'pdf', ocr=False)
        self.assertIn('180', '\n'.join(unit.text for unit in units))
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        image = Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
        stream = io.BytesIO(); image.save(stream, format='PNG')
        units, stats = extract_attachment(stream.getvalue(), 'png')
        text = '\n'.join(unit.text for unit in units)
        self.assertIn('180', text)
        self.assertIn('25', text)
        self.assertIn('60', ''.join(text.split()))
        self.assertTrue('温度' in text or '压力' in text)
        self.assertEqual(stats['images_ocr_processed'], 1)

    def test_ambiguous_ocr_digits_are_excluded_not_guessed(self):
        text, removed = filter_ocr_text('压力25bar\n保温6 0mi n\n压合温度180°C')
        self.assertEqual(removed, 1)
        self.assertNotIn('60', text)
        self.assertNotIn('6 0', text)
        self.assertIn('25bar', text)
        self.assertIn('180°C', text)

    def test_zip_names_are_not_extracted_to_filesystem(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('../../do_not_create.txt', '压合温度180°C，压力25bar，保温60min。'.encode())
        units, _ = extract_attachment(stream.getvalue(), 'zip', ocr=False)
        self.assertIn('25bar', '\n'.join(unit.text for unit in units))
        self.assertTrue(all('do_not_create' not in unit.label for unit in units))

    def test_attachment_content_is_sanitized_after_parsing(self):
        units, _ = extract_attachment('测试甲：压合温度180°C，压力25bar。\n邮箱 contact@example.net'.encode(), 'txt', ocr=False)
        index = IdentityIndex(); index.add('测试甲')
        sanitizer = Sanitizer(index.pattern())
        text = sanitizer.clean('\n'.join(unit.text for unit in units))
        self.assertNotIn('测试甲', text)
        self.assertNotIn('example.net', text)
        self.assertIn('180', text)
        self.assertIn('25bar', text)
        self.assertFalse(sanitizer.residuals(text))


if __name__ == '__main__':
    unittest.main()
