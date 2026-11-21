"""Bounded, local attachment text extraction. Never execute document content."""
from __future__ import annotations

import collections
from dataclasses import dataclass
import io
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
NATIVE = ROOT / '.private/attachment_tools'
os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
_OCR = None
MAX_BYTES = 64 * 1024 * 1024
MAX_CHARACTERS = 500000
MAX_PAGES = 300
MAX_ROWS = 50000
MAX_COLUMNS = 200
IMAGE_TYPES = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tif', 'tiff'}
ALLOWED_TYPES = IMAGE_TYPES | {'pdf', 'docx', 'xlsx', 'xlsm', 'xls', 'doc', 'pptx', 'ppt', 'rtf', 'txt', 'csv', 'html', 'htm', 'xml', 'zip', 'rar', '7z', 'eml'}


class ExtractionError(Exception):
    pass


def file_kind(name, data):
    ext = Path(name).suffix.lower().lstrip('.')
    head = data[:256].lstrip()
    if data.startswith(b'%PDF'):
        return 'pdf'
    if data.startswith(b'\x89PNG'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if data.startswith(b'7z\xbc\xaf\x27\x1c'):
        return '7z'
    if data.startswith(b'Rar!'):
        return 'rar'
    if head.startswith(b'{\\rtf'):
        return 'rtf'
    if head.lower().startswith((b'<!doctype html', b'<html')):
        return 'html'
    if data.startswith(b'PK\x03\x04'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
        if 'word/document.xml' in names:
            return 'docx'
        if 'xl/workbook.xml' in names:
            return 'xlsx'
        if 'ppt/presentation.xml' in names:
            return 'pptx'
        return 'zip'
    if data.startswith(b'\xd0\xcf\x11\xe0'):
        import olefile
        with olefile.OleFileIO(io.BytesIO(data)) as document:
            if document.exists('WordDocument'):
                return 'doc'
            if document.exists('Workbook') or document.exists('Book'):
                return 'xls'
            if document.exists('PowerPoint Document'):
                return 'ppt'
            if document.exists('EncryptedPackage'):
                return 'encrypted_office'
    return ext if ext in ALLOWED_TYPES else 'unsupported'


def decode_text(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    for encoding in ('utf-8-sig', 'gb18030', 'big5', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ExtractionError('text_encoding')


def value_text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


@dataclass
class Unit:
    label: str
    text: str
    method: str = 'native_text'
    confidence: float | None = None


class Extractor:
    def __init__(self, ocr=True):
        self.ocr_enabled = ocr
        self.stats = collections.Counter()
        self.units = []
        self.characters = 0
        self.expanded_bytes = 0
        self.member_count = 0

    def add(self, label, text, method='native_text', confidence=None):
        text = text.strip()
        if not text:
            return
        remaining = MAX_CHARACTERS - self.characters
        if remaining <= 0:
            self.stats['character_limit_reached'] += 1
            return
        if len(text) > remaining:
            self.stats['character_limit_reached'] += 1
            text = text[:remaining]
        # Bounded units retain exact extracted text; following sanitation is separate.
        for start in range(0, len(text), 6000):
            piece = text[start:start + 6000]
            if (self.units and self.units[-1].label == label and self.units[-1].method == method
                    and self.units[-1].confidence == confidence and len(self.units[-1].text) + len(piece) < 6000):
                self.units[-1].text += '\n' + piece
            else:
                self.units.append(Unit(label, piece, method, confidence))
        self.characters += len(text)

    def image_text(self, image, label):
        if not self.ocr_enabled:
            self.stats['ocr_disabled'] += 1
            return
        if image.width * image.height < 12000 or max(image.size) < 240:
            self.stats['small_image_skipped'] += 1
            return
        if image.width * image.height > 20000000:
            self.stats['image_pixel_limit'] += 1
            return
        from gpu_ocr_client import recognize
        result = recognize(image)
        if result is None:
            global _OCR
            if _OCR is None:
                import onnxruntime
                onnxruntime.disable_telemetry_events()
                from rapidocr_onnxruntime import RapidOCR
                _OCR = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False)
            import numpy as np
            result, _ = _OCR(np.ascontiguousarray(np.asarray(image.convert('RGB'))[:, :, ::-1]))
            self.stats['cpu_ocr_images'] += 1
        else:
            self.stats['gpu_ocr_images'] += 1
        self.stats['images_ocr_processed'] += 1
        lines, confidences = [], []
        for row in result or []:
            if float(row[2]) >= .65:
                lines.append(row[1])
                confidences.append(float(row[2]))
            else:
                self.stats['low_confidence_ocr_lines_skipped'] += 1
        if lines:
            self.add(label, '\n'.join(lines), 'local_ocr', min(confidences))

    def embedded_images(self, data, prefix, label):
        from PIL import Image
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            images = [info for info in archive.infolist() if info.filename.startswith(prefix) and not info.is_dir()]
            for number, info in enumerate(images[:100], 1):
                if info.file_size > MAX_BYTES or info.file_size / max(1, info.compress_size) > 500:
                    self.stats['oversize_embedded_image'] += 1
                    continue
                try:
                    with Image.open(io.BytesIO(archive.read(info))) as image:
                        self.image_text(image, label + f'/图片{number}')
                except Exception:
                    self.stats['embedded_image_errors'] += 1
            if len(images) > 100:
                self.stats['embedded_image_limit'] += 1

    def table_rows(self, rows, label):
        headers, buffered = [], []
        size, empty = 0, 0
        for number, values in enumerate(rows, 1):
            if number > MAX_ROWS:
                self.stats['table_row_limit'] += 1
                break
            cells = [f'列{col}={value_text(value)}' for col, value in enumerate(values, 1) if value_text(value)]
            if not cells:
                empty += 1
                continue
            empty = 0
            row = f'行{number} | ' + ' | '.join(cells[:MAX_COLUMNS])
            if len(cells) > MAX_COLUMNS:
                self.stats['table_column_limit'] += 1
            if len(headers) < 3:
                headers.append(row)
            if size + len(row) > 5000 and buffered:
                self.add(label, '\n'.join(buffered))
                buffered = ['表头上下文：'] + headers
                size = sum(map(len, buffered))
            buffered.append(row)
            size += len(row)
            if self.characters >= MAX_CHARACTERS:
                self.stats['character_limit_reached'] += 1
                break
        if buffered:
            self.add(label, '\n'.join(buffered))

    def extract(self, data, kind, label='附件', depth=0):
        if len(data) > MAX_BYTES:
            raise ExtractionError('attachment_size_limit')
        if depth > 2:
            self.stats['archive_depth_limit'] += 1
            return
        if kind in IMAGE_TYPES:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = 20000000
            with Image.open(io.BytesIO(data)) as image:
                self.image_text(image, label + '/图片')
        elif kind == 'pdf':
            import pymupdf
            from PIL import Image
            with pymupdf.open(stream=data, filetype='pdf') as document:
                if document.needs_pass:
                    raise ExtractionError('encrypted_pdf')
                for number in range(min(len(document), MAX_PAGES)):
                    if self.characters >= MAX_CHARACTERS:
                        self.stats['character_limit_reached'] += 1
                        break
                    page = document[number]
                    text = page.get_text(sort=True)
                    self.stats['pdf_pages'] += 1
                    characters = len(re.sub(r'\s', '', text))
                    if characters > 0:
                        self.add(label + f'/第{number + 1}页', text)
                    if characters < 200 and self.ocr_enabled:
                        scale = min(2.0, math.sqrt(19000000 / max(1, page.rect.width * page.rect.height)))
                        if scale < 2:
                            self.stats['pdf_render_downscaled'] += 1
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                        self.image_text(Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples), label + f'/第{number + 1}页')
                if len(document) > MAX_PAGES:
                    self.stats['page_limit_reached'] += 1
        elif kind == 'docx':
            from docx import Document
            document = Document(io.BytesIO(data))
            for number, block in enumerate(document.iter_inner_content(), 1):
                if hasattr(block, 'rows'):
                    self.table_rows(([cell.text for cell in row.cells] for row in block.rows), label + f'/表格{number}')
                elif block.text:
                    self.add(label + '/正文', block.text)
            self.embedded_images(data, 'word/media/', label)
        elif kind in {'xlsx', 'xlsm'}:
            import openpyxl
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
            try:
                for number, sheet in enumerate(workbook.worksheets[:100], 1):
                    max_row = min(sheet.max_row, MAX_ROWS) if sheet.max_row else None
                    max_col = min(sheet.max_column or MAX_COLUMNS, MAX_COLUMNS)
                    if sheet.max_row and sheet.max_row > MAX_ROWS:
                        self.stats['table_row_limit'] += 1
                    if sheet.max_column and sheet.max_column > MAX_COLUMNS:
                        self.stats['table_column_limit'] += 1
                    self.table_rows(sheet.iter_rows(max_row=max_row, max_col=max_col, values_only=True), label + f'/工作表{number}')
                if len(workbook.worksheets) > 100:
                    self.stats['sheet_limit_reached'] += 1
            finally:
                workbook.close()
            self.stats['formula_cache_only'] += 1
            self.embedded_images(data, 'xl/media/', label)
        elif kind == 'xls':
            import xlrd
            workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
            try:
                for number in range(min(workbook.nsheets, 100)):
                    sheet = workbook.sheet_by_index(number)
                    self.table_rows((sheet.row_values(row) for row in range(sheet.nrows)), label + f'/工作表{number + 1}')
            finally:
                workbook.release_resources()
            self.stats['formula_cache_only'] += 1
        elif kind == 'pptx':
            from pptx import Presentation
            from PIL import Image
            presentation = Presentation(io.BytesIO(data))
            for number, slide in enumerate(presentation.slides):
                if number >= MAX_PAGES:
                    self.stats['page_limit_reached'] += 1
                    break
                slide_label = label + f'/幻灯片{number + 1}'
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        self.add(slide_label, shape.text)
                    if shape.has_table:
                        self.table_rows(([cell.text for cell in row.cells] for row in shape.table.rows), slide_label + '/表格')
                    if shape.shape_type == 13:
                        try:
                            with Image.open(io.BytesIO(shape.image.blob)) as image:
                                self.image_text(image, slide_label + '/图片')
                        except Exception:
                            self.stats['embedded_image_errors'] += 1
        elif kind == 'ppt':
            import olefile
            with olefile.OleFileIO(io.BytesIO(data)) as document:
                blob = document.openstream('PowerPoint Document').read()
            parts = []
            def records(start, end, nesting=0):
                if nesting > 32:
                    return
                while start + 8 <= end:
                    flags, record_type, length = struct.unpack_from('<HHI', blob, start)
                    first, last = start + 8, start + 8 + length
                    if last > end:
                        break
                    if flags & 15 == 15:
                        records(first, last, nesting + 1)
                    elif record_type in (4000, 4008):
                        parts.append(blob[first:last].decode('utf-16le' if record_type == 4000 else 'cp1252', errors='replace'))
                    start = last
            records(0, len(blob))
            self.add(label + '/幻灯片文本', '\n'.join(parts))
        elif kind == 'doc':
            if os.name == 'nt':
                raise ExtractionError('legacy_doc_parser_unavailable_on_windows')
            executable = NATIVE / 'usr/bin/antiword'
            if not executable.exists():
                raise ExtractionError('legacy_doc_parser_unavailable')
            with tempfile.TemporaryDirectory(prefix='pcb-attachment-', dir='/tmp') as temporary:
                path = Path(temporary) / 'input.doc'
                path.write_bytes(data)
                environment = dict(os.environ, ANTIWORDHOME=str(NATIVE / 'usr/share/antiword'))
                result = subprocess.run([str(executable), '-m', 'UTF-8.txt', '-w', '0', str(path)],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, env=environment)
                if result.returncode:
                    raise ExtractionError('legacy_doc_parse_error')
                self.add(label + '/正文', result.stdout.decode('utf-8', errors='strict'))
        elif kind in {'txt', 'csv', 'rtf', 'html', 'htm', 'xml'}:
            text = decode_text(data)
            if kind == 'rtf':
                from striprtf.striprtf import rtf_to_text
                text = rtf_to_text(text)
            elif kind in {'html', 'htm'}:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(text, 'html.parser')
                for node in soup(['script', 'style', 'img', 'iframe', 'object', 'head']):
                    node.decompose()
                text = soup.get_text('\n')
            elif kind == 'xml':
                from lxml import etree
                root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                text = '\n'.join(root.itertext())
            self.add(label + '/正文', text)
        elif kind in {'zip', 'rar', '7z'}:
            self.archive(data, kind, label, depth)
        elif kind == 'eml':
            from email import policy
            from email.parser import BytesParser
            message = BytesParser(policy=policy.default).parsebytes(data)
            for number, part in enumerate(message.walk(), 1):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                if part.get_content_type() == 'text/plain':
                    self.add(label + '/嵌入邮件正文', decode_text(payload))
                elif part.get_filename():
                    nested_kind = file_kind(part.get_filename(), payload)
                    self.extract(payload, nested_kind, label + f'/嵌入附件{number}', depth + 1)
        elif kind == 'encrypted_office':
            raise ExtractionError('encrypted_office')
        else:
            self.stats['unsupported_members'] += 1

    def archive(self, data, kind, label, depth):
        if kind == 'zip':
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for number, info in enumerate(archive.infolist(), 1):
                    if info.is_dir():
                        continue
                    if info.flag_bits & 1:
                        self.stats['encrypted_archive_members'] += 1
                        continue
                    self.member_count += 1
                    if self.member_count > 1000 or self.expanded_bytes + info.file_size > 128 * 1024 * 1024:
                        self.stats['archive_limit_reached'] += 1
                        break
                    if info.file_size > MAX_BYTES or info.file_size / max(1, info.compress_size) > 500:
                        self.stats['archive_member_size_limit'] += 1
                        continue
                    self.expanded_bytes += info.file_size
                    try:
                        content = archive.read(info)
                        self.extract(content, file_kind(info.filename, content), label + f'/压缩成员{number}', depth + 1)
                    except Exception:
                        self.stats['archive_member_errors'] += 1
        else:
            library = NATIVE / 'usr/lib/x86_64-linux-gnu/libarchive.so.13'
            if library.exists():
                os.environ['LIBARCHIVE'] = str(library)
            try:
                import libarchive
            except Exception:
                raise ExtractionError('archive_parser_unavailable') from None
            with libarchive.memory_reader(data) as archive:
                for number, entry in enumerate(archive, 1):
                    if not entry.isfile:
                        continue
                    self.member_count += 1
                    if self.member_count > 1000 or entry.size > MAX_BYTES or self.expanded_bytes + entry.size > 128 * 1024 * 1024:
                        self.stats['archive_limit_reached'] += 1
                        break
                    content = bytearray()
                    for block in entry.get_blocks():
                        if len(content) + len(block) > MAX_BYTES:
                            raise ExtractionError('archive_expansion_limit')
                        content.extend(block)
                    self.expanded_bytes += len(content)
                    self.extract(bytes(content), file_kind(entry.pathname, content), label + f'/压缩成员{number}', depth + 1)


def extract_attachment(data, kind, ocr=True):
    extractor = Extractor(ocr=ocr)
    extractor.extract(data, kind)
    return extractor.units, dict(extractor.stats)
