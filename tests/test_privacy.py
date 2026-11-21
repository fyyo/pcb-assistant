"""Synthetic fixtures only. Never put real mail fragments into tests."""
import re
import unittest
from types import SimpleNamespace

from pcb_assistant.privacy.mail import (IdentityIndex, Sanitizer, normalize, opaque_id,
                          redact_residual_person_names, technical_topics)


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.index=IdentityIndex()
        self.index.add('测试甲')
        self.index.contact('Alice Example <alice.example@example.org>')
        self.cleaner=Sanitizer(self.index.pattern())

    def test_headers_and_forwarded_contacts(self):
        text='发件人: 测试甲 <alice.example@example.org>\n收件人: 测试乙\n\n压合压力 25 bar\nFrom: Another <another@example.net>\n\tHidden Recipient\n\n保温 60 min'
        out=self.cleaner.clean(text)
        for secret in ('测试甲','测试乙','alice.example','another','Hidden Recipient'):
            self.assertNotIn(secret,out)
        self.assertIn('25 bar',out)
        self.assertIn('60 min',out)

    def test_process_parameters_and_standards(self):
        text='压合温度 180℃，压力 25 kg/cm²，真空 -0.095 MPa，保温 60 min；升温 2.5℃/min；铜厚 35 μm；Tg 170℃；FR-4，FR406，IPC-4101。'
        out=self.cleaner.clean(text)
        for parameter in ('180°C','25 kg/cm2','-0.095 MPa','60 min','2.5°C/min','35 μm','170°C','FR-4','FR406','IPC-4101'):
            self.assertIn(parameter,out)
        self.assertIn('压合',technical_topics(out))
        self.assertFalse(self.cleaner.residuals(out))

    def test_contact_and_business_data(self):
        text='测试甲 请检查，Alice Example 已确认。\n电话: 13800138000\n另电 +86 139 0000 0000，座机 0755-12345678。\n客户: 测试客户；项目编号: ZX-123456；订单号: 202412345678\n访问 https://example.net/private?key=abc\n密码: synthetic_secret\n地址: 测试省测试市测试路123号\n预算 ￥12,000；2024-04-20 09:30；材料均由虚构科技有限公司提供。'
        out=self.cleaner.clean(text)
        for secret in ('测试甲','Alice','13800138000','139 0000','12345678','测试客户','ZX-123456','202412345678','example.net','synthetic_secret','123号','12,000','2024-04-20','虚构科技'):
            self.assertNotIn(secret,out)
        self.assertFalse(self.cleaner.residuals(out))

    def test_attachment_paths_and_inline_names(self):
        out=self.cleaner.clean('文件 secret_project.xlsx，图片 customer_logo.png，路径 C:\\private\\client\\file.pdf\n压合改善')
        for secret in ('secret_project','customer_logo','private','client'):
            self.assertNotIn(secret,out)
        self.assertIn('压合改善',out)

    def test_signature_stops_at_quoted_message(self):
        text='压合保温 60 min\nBest regards,\nPrivate Person\nPrivate Company\n-----Original Message-----\nFrom: Somebody <somebody@example.net>\n\n原方案压力 20 bar'
        out=self.cleaner.clean(text)
        self.assertNotIn('Private',out)
        self.assertIn('60 min',out)
        self.assertIn('20 bar',out)

    def test_unicode_and_obfuscation(self):
        out=self.cleaner.clean('a\u200blicｅ＠example.com；person [at] example [dot] net；＋８６ １３８００１３８０００')
        self.assertFalse(self.cleaner.residuals(out))
        self.assertNotIn('example',out)
        self.assertNotIn('13800138000',out)

    def test_english_addresses_and_dates(self):
        out=self.cleaner.clean('123 Fictional Road\nMeeting April 20, 2024\nReviewed 20-Apr-2024')
        self.assertNotIn('Fictional',out)
        self.assertNotIn('2024',out)

    def test_audit_detects_unsanitized_output(self):
        findings=self.cleaner.residuals('测试甲 mailto:a@example.org 13800138000 2024/4/20')
        self.assertTrue({'known_identity','email','phone','date'}.issubset(findings))

    def test_opaque_identifiers(self):
        self.assertEqual(opaque_id(b'a','123'),opaque_id(b'a','123'))
        self.assertNotEqual(opaque_id(b'a','123'),opaque_id(b'b','123'))
        self.assertTrue(re.fullmatch(r'mail_[0-9a-f]{24}',opaque_id(b'a','123')))

    def test_identity_overlap_boundaries_and_unicode_offsets(self):
        idx=IdentityIndex()
        for term in ('测试甲','测试甲乙','Ann','STRASSE'):
            idx.add(term)
        matcher=idx.pattern()
        result,count=matcher.subn('[身份]','测试甲乙与测试甲。Ann Annual straße')
        self.assertEqual(result,'[身份]与[身份]。[身份] Annual [身份]')
        self.assertEqual(count,4)

    def test_ner_and_boundary_context(self):
        class FakeNLP:
            def __call__(self,text):
                return SimpleNamespace(ents=[SimpleNamespace(start_char=m.start(),end_char=m.end(),text=m[0],label_='PERSON') for m in re.finditer('边界姓名',text)])
        sanitizer=Sanitizer(re.compile(r'(?!)'),FakeNLP(),None)
        out=sanitizer.clean('甲'*1798+'边界姓名'+'乙'*100)
        self.assertNotIn('边界姓名',out)
        self.assertIn('[实体]',out)

    def test_residual_person_names_without_dropping_technical_terms(self):
        text = ('2月底时苏鹏副理回复\n与品保闫经理确认\n按林经理要求\n'
                '3,维护课长签核\n專案副理\n高級經理\n不含副课长\n'
                '[身份]\nR H XU 徐榮華\n654665\n孔破\n板彎/板翹')
        out = redact_residual_person_names(text)
        for secret in ('苏鹏', '闫', '徐榮華', '林经理'):
            self.assertNotIn(secret, out)
        for kept in ('维护课长', '專案副理', '高級經理', '不含副课长', '孔破', '板彎'):
            self.assertIn(kept, out)

    def test_greeting_names_are_redacted_but_technical_labels_kept(self):
        text = 'Dear 王五\n張三,李四:\n欣瑤:\n孔破:\n备注:'
        out = redact_residual_person_names(text)
        for secret in ('王五', '張三', '李四', '欣瑤'):
            self.assertNotIn(secret, out)
        for kept in ('孔破:', '备注:'):
            self.assertIn(kept, out)


if __name__=='__main__':
    unittest.main()
