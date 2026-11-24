"""Synthetic image traversal, privacy, numeric integrity and append-only OCR checks."""
from collections import Counter
from contextlib import closing,redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from pcb_assistant.catalog.common import digest_file
from pcb_assistant.ingestion.images import ImageEnumerator,image_fingerprint,image_eligibility
from pcb_assistant.privacy.ocr import OcrTextSanitizer,OcrPrivacyRejected
from pcb_assistant.ingestion.ocr import sanitized_result,text_pieces,validate_checkpoint_result,publish_supplement


def png(size=(400,100),color='white'):
    stream=io.BytesIO()
    Image.new('RGB',size,color).save(stream,format='PNG')
    return stream.getvalue()


class ImageSourceTests(unittest.TestCase):
    def test_pixels_deduplicate_encodings_and_dimensions_are_part_of_identity(self):
        image=Image.new('RGB',(400,100),'white')
        stream=io.BytesIO();image.save(stream,format='BMP')
        a=list(ImageEnumerator().enumerate(png(),'png'))[0]
        b=list(ImageEnumerator().enumerate(stream.getvalue(),'bmp'))[0]
        self.assertEqual(image_fingerprint(a.image),image_fingerprint(b.image))
        self.assertNotEqual(image_fingerprint(image),image_fingerprint(Image.new('RGB',(200,200),'white')))
        self.assertIsNone(image_eligibility(image))
        self.assertEqual(image_eligibility(Image.new('RGB',(100,20))),'small_image_skipped')

    def test_all_office_bitmap_media_have_opaque_locators_and_vector_gaps(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('xl/media/private_person.png',png())
            archive.writestr('xl/media/private_person.emf',b'not_bitmap')
            archive.writestr('xl/worksheets/sheet1.xml',b'<synthetic/>')
        engine=ImageEnumerator()
        images=list(engine.enumerate(stream.getvalue(),'xlsx'))
        self.assertEqual(len(images),1)
        self.assertEqual(images[0].locator,'attachment/media:1')
        self.assertNotIn('private_person',images[0].locator)
        self.assertEqual(engine.counts['unsupported_vector_or_media'],1)

    def test_archive_and_eml_members_are_not_extracted_to_paths(self):
        from email.message import EmailMessage
        mail=EmailMessage()
        mail.set_content('private native text must not be extracted here')
        mail.add_attachment(png(),maintype='image',subtype='png',filename='private.png')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('../../never_write.eml',mail.as_bytes())
        images=list(ImageEnumerator().enumerate(stream.getvalue(),'zip'))
        self.assertEqual(len(images),1)
        self.assertNotIn('private',images[0].locator)
        self.assertEqual(images[0].image.size,(400,100))

    def test_sparse_pdf_page_is_rendered_and_dense_page_embedded_image_is_seen(self):
        data=(Path(__file__).parent/'data'/'pdf_sparse_and_dense_pages.pdf').read_bytes()
        engine=ImageEnumerator()
        images=list(engine.enumerate(data,'pdf'))
        self.assertEqual(engine.counts['pdf_pages_checked'],2)
        self.assertEqual(engine.counts['pdf_sparse_pages_rendered'],1)
        self.assertEqual(engine.counts['pdf_native_text_pages_preserved'],1)
        self.assertEqual(len(images),2)
        self.assertIn('/page:1/render',images[0].locator)
        self.assertIn('/page:2/image:1',images[1].locator)

    def test_multiple_tiff_pages_are_enumerated(self):
        stream=io.BytesIO()
        a,b=Image.new('RGB',(400,100),'white'),Image.new('RGB',(400,100),'black')
        a.save(stream,format='TIFF',save_all=True,append_images=[b])
        images=list(ImageEnumerator().enumerate(stream.getvalue(),'tiff'))
        self.assertEqual([x.locator for x in images],['attachment/image/frame:1','attachment/image/frame:2'])


class OcrPrivacyTests(unittest.TestCase):
    def test_new_raw_ocr_contacts_are_removed_and_recognized_parameters_retained(self):
        cleaner=OcrTextSanitizer({'陈欣瑶','Alice Example'})
        raw='陈欣瑶确认\n压合温度180℃ 压力25bar 保温60min\n邮箱 person@example.org\n日期 2025/11/15'
        text,audit=cleaner.sanitize(raw)
        self.assertNotIn('陈欣瑶',text)
        self.assertNotIn('example.org',text)
        self.assertNotIn('2025/11/15',text)
        self.assertIn('180℃',text);self.assertIn('25bar',text);self.assertIn('60min',text)
        self.assertTrue(audit['engineering_expression_preserved'])
        self.assertGreater(audit['numeric_tokens_redacted_as_private'],0)
        self.assertFalse(cleaner.residuals(text))

    def test_sensitive_field_cannot_erase_a_parameter(self):
        cleaner=OcrTextSanitizer(set())
        with self.assertRaisesRegex(OcrPrivacyRejected,'sensitive_span_overlaps_engineering_expression'):
            cleaner.sanitize('密码：保温60min，压力25bar')

    def test_ner_removes_unknown_person_and_preserves_technical_entities_and_values(self):
        raw='李安和确认 真空 0.095 MPa FR-4'
        class Model:
            lang='zh'
            def __call__(self,text):
                entities=[]
                for value in ('李安和','真空','0.095 MPa','FR-4'):
                    if value in text:
                        start=text.index(value)
                        entities.append(SimpleNamespace(text=value,start_char=start,end_char=start+len(value),label_='ORG'))
                return SimpleNamespace(ents=entities)
        text,audit=OcrTextSanitizer(set(),[Model()]).sanitize(raw)
        self.assertNotIn('李安和',text)
        self.assertIn('真空 0.095 MPa FR-4',text)
        self.assertTrue(audit['engineering_expression_preserved'])

    def test_conflicting_image_is_quarantined_without_persisting_raw_text(self):
        raw={'pixel_sha256':'a'*64,'status':'recognized','text':'password: 保温60min',
             'minimum_confidence':.99,'recognized_lines':1,'low_confidence_lines_skipped':0,'seconds':1}
        result=sanitized_result(raw,OcrTextSanitizer(set()))
        self.assertEqual(result['status'],'privacy_quarantined')
        self.assertNotIn('text',result)
        self.assertNotIn('password',json.dumps(result))
        validate_checkpoint_result(result)

    def test_readback_detects_numbers_tampered_and_spaced_digits_are_not_guessed(self):
        raw={'pixel_sha256':'b'*64,'status':'recognized','text':'压力25bar\n保温6 0min\n温度180℃',
             'minimum_confidence':.99,'recognized_lines':3,'low_confidence_lines_skipped':1,'seconds':1}
        result=sanitized_result(raw,OcrTextSanitizer(set()))
        self.assertEqual(result['status'],'sanitized')
        self.assertIn('6 0min',result['text'])
        self.assertEqual(result['audit']['spaced_numeric_lines'],1)
        validate_checkpoint_result(result)
        result['text']=result['text'].replace('25bar','26bar')
        with self.assertRaisesRegex(ValueError,'checkpoint_engineering_hash_mismatch'):
            validate_checkpoint_result(result)

    def test_piece_boundaries_preserve_complete_lines_and_numbers(self):
        text=('真空0.095 MPa\n'*1000)+'尾部180℃'
        pieces=text_pieces(text)
        self.assertEqual(''.join(pieces),text)
        self.assertTrue(all(len(x)<=6000 for x in pieces))
        self.assertTrue(all(x.endswith('\n') for x in pieces[:-1]))


class OcrPublicationTests(unittest.TestCase):
    def test_checkpoint_readback_publication_and_incremental_parent_relationship(self):
        from pcb_assistant.catalog.build import build
        from pcb_assistant.catalog.common import stable_id
        from pcb_assistant.catalog.incremental import ingest
        from pcb_assistant.catalog.cli import validate
        from .test_catalog import fixture,write_rows
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            fixture(root)
            base=root/'data/phase3_catalog_test'
            with redirect_stdout(io.StringIO()):
                build(root,base)
            before=digest_file(base/'catalog.sqlite3')
            clean=root/'data/clean'
            write_rows(root,'data/clean/documents.jsonl',[
                {'document_id':'att_a','format':'png','parent_source_ids':['mail_a']}])
            write_rows(root,'data/clean/engineering_candidates.jsonl',[])
            inventory=root/'data/inventory'
            digest='c'*64
            write_rows(root,'data/inventory/components.jsonl',[
                {'document_id':'att_a','pixel_sha256':digest,'locator':'attachment/image/frame:1','status':'eligible'},
                {'document_id':'att_a','pixel_sha256':digest,'locator':'attachment/image/frame:2','status':'eligible'}])
            (inventory/'report.json').write_text(json.dumps({'unique_eligible_images':1,
                'statistics':{},'artifacts':{'components.jsonl':{
                    'sha256':digest_file(inventory/'components.jsonl')}}}),encoding='utf-8')
            output=root/'data/ocr_test'
            output.mkdir()
            cleaner=OcrTextSanitizer(set())
            image=sanitized_result({'pixel_sha256':digest,'status':'recognized',
                'text':'lamination pressure 25bar temperature 180C',
                'minimum_confidence':.99,'recognized_lines':1,'low_confidence_lines_skipped':0,'seconds':1},cleaner)
            self.assertEqual(image['status'],'sanitized')
            with closing(sqlite3.connect(output/'checkpoint.sqlite3')) as con:
                con.execute('CREATE TABLE images(pixel_sha256 TEXT PRIMARY KEY,result_json TEXT NOT NULL)')
                con.execute('INSERT INTO images VALUES (?,?)',(digest,json.dumps(image)))
                con.commit()
                # Close/reopen models the durable checkpoint resume path.
            with closing(sqlite3.connect(output/'checkpoint.sqlite3')) as con:
                with patch('pcb_assistant.ingestion.ocr.CLEAN_BASE',clean),patch(
                        'pcb_assistant.ingestion.ocr.verified_sources',return_value={}),patch(
                        'pcb_assistant.ingestion.ocr.verified_clean_base',return_value={}),patch(
                        'pcb_assistant.ingestion.ocr.snapshot',return_value={}),redirect_stdout(io.StringIO()):
                    report=publish_supplement(output,inventory,con,cleaner,{}, {},Counter(),{}, {})
            self.assertEqual(report['statistics']['exported_units'],1)
            record=json.loads((output/'records.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(record['text'],image['text'])
            self.assertEqual(len(record['metadata']['component_locators']),2)
            candidate=root/'data/phase3_catalog_ocr_test'
            with redirect_stdout(io.StringIO()):
                result=ingest(base,output/'manifest.json',candidate,root=root)
            self.assertEqual(result['batch']['new_entities'],1)
            self.assertEqual(result['batch']['missing_links'],0)
            self.assertEqual(validate(candidate)['status'],'passed')
            self.assertEqual(digest_file(base/'catalog.sqlite3'),before)
            with closing(sqlite3.connect(candidate/'catalog.sqlite3')) as con:
                target=stable_id('ATTACHMENT','att_a')
                source=stable_id('KNOWLEDGE','attachment',record['source_id'])
                self.assertEqual(con.execute('SELECT COUNT(*) FROM relationships WHERE from_id=? AND to_id=?',
                                             (source,target)).fetchone()[0],1)
            import scripts.audits.verify_ocr_supplement as verifier
            (root/'docs').mkdir()
            (root/'docs/OCR_BASELINE_SNAPSHOTS_20261115.json').write_text(
                '{"snapshots":{}}',encoding='utf-8')
            with patch.object(verifier,'ROOT',root),patch.object(verifier,'build_identity_index',
                    return_value=SimpleNamespace(terms=set())),patch.object(verifier,'connect_archive',
                    side_effect=lambda _:sqlite3.connect(':memory:')),redirect_stdout(io.StringIO()):
                checked=verifier.verify(output,inventory,candidate,base)
            self.assertEqual(checked['status'],'passed')
            self.assertEqual(checked['exported_units'],1)


if __name__=='__main__':
    unittest.main()
