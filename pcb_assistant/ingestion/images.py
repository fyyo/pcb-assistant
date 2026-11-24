"""Enumerate bounded bitmap components without exporting names or native text."""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import io
import math
from pathlib import Path
import re
import struct
import zipfile

from PIL import Image, ImageOps

from pcb_assistant.ingestion.extract import MAX_BYTES, IMAGE_TYPES, file_kind

MAX_PIXELS = 20_000_000
MAX_PAGES = 1000
MAX_COMPONENTS = 2000
MAX_EXPANDED = 128*1024*1024


@dataclass
class ImageComponent:
    locator: str
    image: Image.Image


def image_fingerprint(image):
    """Dimensions plus RGB pixels deduplicate equal images across encodings."""
    image=image.convert('RGB')
    return sha256(struct.pack('>II',*image.size)+image.tobytes()).hexdigest()


def image_eligibility(image):
    if image.width*image.height>MAX_PIXELS:
        return 'image_pixel_limit'
    if image.width*image.height<12000 or max(image.size)<240:
        return 'small_image_skipped'
    return None


class ImageEnumerator:
    def __init__(self):
        self.counts=Counter()
        self.expanded=0
        self.members=0

    def bitmap(self,data,locator):
        if len(data)>MAX_BYTES:
            self.counts['oversize_image']+=1
            return
        try:
            with Image.open(io.BytesIO(data)) as opened:
                if opened.width*opened.height>MAX_PIXELS:
                    self.counts['image_pixel_limit']+=1
                    return
                # GIF animations usually repeat a logo; OCR the first visible
                # frame. Multipage TIFFs represent distinct source pages.
                frames=min(getattr(opened,'n_frames',1),MAX_PAGES) if opened.format=='TIFF' else 1
                for frame in range(frames):
                    opened.seek(frame)
                    if opened.width*opened.height>MAX_PIXELS:
                        self.counts['image_pixel_limit']+=1
                        continue
                    image=ImageOps.exif_transpose(opened).convert('RGB')
                    yield ImageComponent(locator+(f'/frame:{frame+1}' if frames>1 else ''),image)
                if getattr(opened,'n_frames',1)>frames:
                    self.counts['additional_animation_frames_not_processed' if opened.format=='GIF' else 'frame_limit']+=getattr(opened,'n_frames',1)-frames
        except Exception:
            self.counts['bitmap_decode_errors']+=1

    def office_media(self,data,prefix,locator):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos=[x for x in archive.infolist() if x.filename.startswith(prefix) and not x.is_dir()]
            for number,info in enumerate(infos[:MAX_COMPONENTS],1):
                extension=Path(info.filename).suffix.lower().lstrip('.')
                if extension not in IMAGE_TYPES:
                    self.counts['unsupported_vector_or_media']+=1
                    continue
                if (info.file_size>MAX_BYTES or info.file_size/max(1,info.compress_size)>500
                        or self.expanded+info.file_size>MAX_EXPANDED):
                    self.counts['embedded_media_resource_limit']+=1
                    continue
                self.expanded+=info.file_size
                yield from self.bitmap(archive.read(info),f'{locator}/media:{number}')
            self.counts['embedded_media_count_limit']+=max(0,len(infos)-MAX_COMPONENTS)

    def legacy_media(self,data,locator):
        """Find PNG/JPEG payloads in bounded OLE image streams; never execute OLE."""
        import olefile
        with olefile.OleFileIO(io.BytesIO(data)) as document:
            streams=document.listdir(streams=True,storages=False)
            for stream_number,path in enumerate(streams,1):
                if self.expanded>=MAX_EXPANDED:
                    self.counts['legacy_stream_resource_limit']+=1
                    break
                size=document.get_size(path)
                if size>MAX_BYTES or self.expanded+size>MAX_EXPANDED:
                    self.counts['legacy_stream_resource_limit']+=1
                    continue
                content=document.openstream(path).read()
                self.expanded+=len(content)
                end=0
                for number,match in enumerate(re.finditer(b'\x89PNG\r\n\x1a\n|\xff\xd8\xff',content),1):
                    if match.start()<end:
                        continue
                    if number>MAX_COMPONENTS:
                        self.counts['legacy_image_count_limit']+=1
                        break
                    if match[0].startswith(b'\x89PNG'):
                        finish=content.find(b'IEND',match.end())
                        finish=finish+8 if finish>=0 else -1
                    else:
                        finish=content.find(b'\xff\xd9',match.end())
                        finish=finish+2 if finish>=0 else -1
                    if finish<0 or finish-match.start()>MAX_BYTES:
                        self.counts['incomplete_legacy_bitmap']+=1
                        continue
                    before=self.counts['bitmap_decode_errors']
                    yield from self.bitmap(content[match.start():finish],f'{locator}/stream:{stream_number}/bitmap:{number}')
                    if self.counts['bitmap_decode_errors']==before:
                        end=finish
            self.counts['legacy_vector_and_unknown_blips_not_decoded']+=1

    def enumerate(self,data,kind,locator='attachment',depth=0):
        if len(data)>MAX_BYTES or depth>2:
            self.counts['attachment_or_depth_limit']+=1
            return
        try:
            if kind in IMAGE_TYPES:
                yield from self.bitmap(data,locator+'/image')
            elif kind in ('xlsx','xlsm','docx','pptx'):
                prefix={'xlsx':'xl/media/','xlsm':'xl/media/','docx':'word/media/','pptx':'ppt/media/'}[kind]
                yield from self.office_media(data,prefix,locator)
            elif kind in ('xls','doc','ppt'):
                yield from self.legacy_media(data,locator)
            elif kind=='pdf':
                from pcb_assistant.ingestion import pdf as pdf_reader
                try:
                    document=pdf_reader.open_document(data)
                except pdf_reader.EncryptedPdf:
                    self.counts['encrypted_pdf']+=1
                    return
                try:
                    for number in range(min(len(document),MAX_PAGES)):
                        self.counts['pdf_pages_checked']+=1
                        text=document.page_text(number)
                        if len(re.sub(r'\s','',text))<200:
                            # Render annotations, vectors and raster together.
                            width,height=document.page_size(number)
                            scale=min(2.,math.sqrt(19_000_000/max(1,width*height)))
                            self.counts['pdf_sparse_pages_rendered']+=1
                            yield ImageComponent(f'{locator}/page:{number+1}/render',document.render_page(number,scale))
                        else:
                            self.counts['pdf_native_text_pages_preserved']+=1
                            payloads,decode_errors=document.page_images(number)
                            self.counts['pdf_image_decode_errors']+=decode_errors
                            for image_number,payload in enumerate(payloads[:MAX_COMPONENTS],1):
                                yield from self.bitmap(payload,f'{locator}/page:{number+1}/image:{image_number}')
                            self.counts['pdf_image_count_limit']+=max(0,len(payloads)-MAX_COMPONENTS)
                    self.counts['pdf_page_limit']+=max(0,len(document)-MAX_PAGES)
                finally:
                    document.close()
            elif kind=='zip':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for number,info in enumerate(archive.infolist(),1):
                        if info.is_dir():
                            continue
                        self.members+=1
                        if self.members>1000 or self.expanded+info.file_size>MAX_EXPANDED:
                            self.counts['archive_resource_limit']+=1
                            break
                        if info.flag_bits&1 or info.file_size>MAX_BYTES or info.file_size/max(1,info.compress_size)>500:
                            self.counts['archive_member_skipped']+=1
                            continue
                        self.expanded+=info.file_size
                        content=archive.read(info)
                        yield from self.enumerate(content,file_kind(info.filename,content),f'{locator}/member:{number}',depth+1)
            elif kind=='eml':
                from email import policy
                from email.parser import BytesParser
                message=BytesParser(policy=policy.default).parsebytes(data)
                for number,part in enumerate(message.walk(),1):
                    if part.get_content_maintype()=='multipart':
                        continue
                    payload=part.get_payload(decode=True)
                    if not payload:
                        continue
                    if part.get_content_maintype()=='image' or part.get_filename():
                        yield from self.enumerate(payload,file_kind(part.get_filename() or part.get_content_subtype(),payload),f'{locator}/part:{number}',depth+1)
            else:
                self.counts['no_supported_image_container']+=1
        except Exception:
            self.counts['component_enumeration_errors']+=1
