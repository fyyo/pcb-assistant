"""Sanitize new OCR text while retaining recognized engineering expressions."""
from collections import Counter
from hashlib import sha256
import json
import re

import mail_privacy as privacy
from candidate_privacy import CandidateIdentityCleaner,ENGINEERING_EXPRESSION,TECHNICAL_PHRASE,_ENGINEERING_CODE
from scripts.data.rewash_candidate_identities import NUMBERS


SENSITIVE_PATTERNS=(
    ('credential',privacy.CREDENTIAL,'[凭据]'),
    ('obfuscated_email',privacy.OBFUSCATED_EMAIL,'[邮箱]'),
    ('email',privacy.EMAIL,'[邮箱]'),('url',privacy.URL,'[网址]'),
    ('path',privacy.PATH,'[内部路径]'),('file',privacy.FILE,'[附件]'),
    ('date',privacy.DATE,'[日期]'),('english_date',privacy.EN_DATE,'[日期]'),
    ('phone',privacy.PHONE,'[电话]'),('ip',privacy.IP,'[地址]'),
    ('long_id',privacy.LONG_ID,'[编号]'),('address',privacy.ADDRESS,'[地址]'),
    ('english_address',privacy.EN_ADDRESS,'[地址]'),('organization',privacy.ORG,'[实体]'),
    ('business',privacy.BUSINESS,'[业务信息]'),
)


def expression_hash(pattern,text):
    return sha256(json.dumps(pattern.findall(text),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


class OcrPrivacyRejected(ValueError):
    pass


class OcrTextSanitizer:
    def __init__(self,terms,models=()):
        self.contacts=CandidateIdentityCleaner(terms)
        self.models=models
        self.counts=Counter()

    @staticmethod
    def _protected(text):
        return [m.span() for pattern in (ENGINEERING_EXPRESSION,_ENGINEERING_CODE,privacy.PLACEHOLDER)
                for m in pattern.finditer(text)]

    def _rules(self,text):
        for label,pattern,replacement in SENSITIVE_PATTERNS:
            spans=[]
            protected=self._protected(text)
            for match in pattern.finditer(text):
                # Existing placeholders and full process expressions cannot be
                # consumed by a broad credential, business or filename match.
                if any(match.start()<end and match.end()>start for start,end in protected):
                    raise OcrPrivacyRejected('sensitive_span_overlaps_engineering_expression')
                spans.append(match.span())
            for start,end in reversed(spans):
                text=text[:start]+replacement+text[end:]
            self.counts['redacted_'+label]+=len(spans)
        return text

    def _ner(self,text):
        spans=[]
        protected=self._protected(text)
        for start in range(0,len(text),1800):
            lo,hi=max(0,start-120),min(len(text),start+1920)
            chunk=text[lo:hi]
            for model in self.models:
                language=getattr(model,'lang','')
                if language=='zh' and not privacy.CJK.search(chunk):
                    continue
                if language=='en' and not re.search(r'[A-Za-z]{3,}',chunk):
                    continue
                for entity in model(chunk).ents:
                    if entity.label_ not in ('PERSON','PER','ORG','GPE','LOC','FAC','NORP'):
                        continue
                    a,b=lo+entity.start_char,lo+entity.end_char
                    if (re.search(r'\d',entity.text) or TECHNICAL_PHRASE.search(entity.text)
                            or any(a<end and b>begin for begin,end in protected)):
                        continue
                    spans.append((a,b))
        merged=[]
        for a,b in sorted(set(spans)):
            if merged and a<=merged[-1][1]:
                merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else:
                merged.append((a,b))
        for a,b in reversed(merged):
            text=text[:a]+'[实体]'+text[b:]
        self.counts['ner_identity_spans']+=len(merged)
        return text

    def sanitize(self,raw):
        if not isinstance(raw,str) or len(raw)>2_000_000:
            raise OcrPrivacyRejected('ocr_text_resource_limit')
        original=raw
        baseline_numeric=None
        baseline_codes=None
        for _ in range(8):
            before=raw
            raw=self._rules(raw)
            # Only explicitly recorded privacy masks may remove numeric text.
            # Name rules and NER must leave the remaining numeric sequence intact.
            numbers=NUMBERS.findall(raw)
            codes=_ENGINEERING_CODE.findall(raw)
            raw=self.contacts.clean(raw)
            raw=self._ner(raw)
            if numbers!=NUMBERS.findall(raw) or codes!=_ENGINEERING_CODE.findall(raw):
                raise OcrPrivacyRejected('identity_cleanup_changed_recognized_numbers_or_codes')
            if raw==before:
                break
        else:
            raise OcrPrivacyRejected('ocr_privacy_did_not_converge')
        if ENGINEERING_EXPRESSION.findall(original)!=ENGINEERING_EXPRESSION.findall(raw):
            raise OcrPrivacyRejected('recognized_engineering_expressions_changed')
        residuals=self.residuals(raw)
        if residuals:
            raise OcrPrivacyRejected('ocr_identity_residual')
        return raw,{
            'recognized_text_sha256':sha256(original.encode()).hexdigest(),
            'engineering_expression_sha256':expression_hash(ENGINEERING_EXPRESSION,raw),
            'engineering_expression_preserved':True,
            'output_numeric_sequence_sha256':expression_hash(NUMBERS,raw),
            'output_codes_sha256':expression_hash(_ENGINEERING_CODE,raw),
            'numeric_tokens_redacted_as_private':len(NUMBERS.findall(original))-len(NUMBERS.findall(raw)),
            'spaced_numeric_lines':sum(bool(re.search(r'\d[ \t]+\d',line)) for line in raw.splitlines()),
            'ocr_values_independently_verified':False,
        }

    def residuals(self,text):
        result=Counter(self.contacts.residuals(text))
        plain=privacy.PLACEHOLDER.sub(' ',text)
        for label,pattern,_ in SENSITIVE_PATTERNS:
            result[label]+=sum(1 for _ in pattern.finditer(plain))
        return {k:v for k,v in result.items() if v}
