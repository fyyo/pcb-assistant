"""Conservative identity-only repair of already sanitized candidate text.

Never normalize engineering text or log identity terms/source snippets here.
"""
from collections import Counter
from bisect import bisect_right
import re

import mail_privacy as privacy


_TECHNICAL = set(privacy._TECHNICAL_LABELS) | set(privacy._GENERIC_NAME_WORDS) | {
    '温度', '压力', '真空', '保温', '升温', '降温', '树脂', '玻纤', '铜箔',
    '分层', '缺胶', '层偏', '翘曲', '层压', '高温', '低温', '固化', '流动',
    '常温', '常压', '正压', '负压', '测试', '压合', '半固化', '薄板', '厚板',
    'resin', 'press', 'pressure', 'temperature', 'vacuum', 'lamination', 'prepreg',
    'cure', 'curing', 'flow', 'material', 'process', 'quality', 'engineering',
    'test', 'board', 'core', 'foil', 'copper', 'glass', 'time', 'min', 'max',
    'pa', 'kpa', 'mpa', 'bar', 'psi', 'mm', 'cm', 'um', 'μm', 'µm',
    'sec', 'second', 'minute', 'hour', 'spc', 'fmea', 'iqc', 'oqc', 'ipqc',
    'fr', 'ipc', 'tg', 'cti', 'torr', 'atm',
}
_TECHNICAL = {term.casefold() for term in _TECHNICAL}
ENGINEERING_EXPRESSION = re.compile(
    r'(?i)(?:[-+]?\d+(?:\.\d+)?\s*(?:℃|°C|MPa|kPa|bar|psi|kg/cm[²2]|mm|[μµu]m|min|sec|%))|'
    r'(?:\b(?:FR-?4|IPC-?\d+(?:/\d+)?)\b)')
TECHNICAL_PHRASE = re.compile('|'.join(
    re.escape(term) if re.search(r'[\u3400-\u9fff]',term) else r'\b'+re.escape(term)+r'\b'
    for term in sorted(_TECHNICAL,key=lambda term:(-len(term),term))), re.I)
_ENGINEERING_CODE = re.compile(
    r'(?<![A-Za-z0-9_])[A-Za-z]{1,8}[-_/]?\d+(?:[./_-]\d+)*[A-Za-z0-9_/-]*(?![A-Za-z0-9_])')
_PROTECTED_CODE = re.compile(r'(?i)^(?:FR[- ]?\d+|IPC[- ]?\d+(?:/\d+)?|ISO[- ]?\d+|'
                             r'Tg\d+|CTI\d+|PP|PCB|HDI|PTFE|CAF|CTE|DSC|TMA|DMA)$')
_CONTACT_LABEL = re.compile(
    r'(?P<label>编制|編制|编写|編寫|审核|審核|核准|批准|复核|覆核|签核|簽核|'
    r'签名|簽名|签字|簽字|姓名|联系人|聯絡人|负责人|負責人|承办人|承辦人)'
    r'(?P<sep>[ \t]*[:：][ \t]*)'
    r'(?P<name>[\u3400-\u9fff]{2,3})(?=$|[\s，,。；;、/])', re.MULTILINE)
_ALIAS_CONTEXT = re.compile(
    r'(?P<prefix>^|[\s，,。；;：:、]|请|請|由|与|與|找|给|給|向|和)'
    r'(?P<name>[\u3400-\u9fff]{2})'
    r'(?P<suffix>[ \t]*(?:回复|回覆|确认|確認|提供|協助|协助|安排|反馈|反饋|负责|負責|签核|簽核|'
    r'收到|您好|你好|[:：][ \t]*(?:\n|$)))', re.MULTILINE)
_ROLE = '|'.join(sorted(privacy.ROLE_TITLES,key=len,reverse=True))
_NAME_BEFORE_ROLE = re.compile(
    r'(?P<prefix>^|[\s，,。；;：:、/()（）]|请|請|由|给|給|与|與|及|和|致)'
    r'(?P<name>[\u3400-\u9fff]{1,3})(?P<space>[ \t]*)(?P<role>'+_ROLE+r')')
_NAME_AFTER_ROLE = re.compile(
    r'(?P<role>'+_ROLE+r')(?P<sep>[ \t]*[:：]?[ \t]*)'
    r'(?P<name>[\u3400-\u9fff]{2,3})')
_GREETING_CJK = re.compile(
    r'(?i)^(?P<prefix>[ \t]*(?:dear|hi|hello)[ \t]+)'
    r'(?P<name>[\u3400-\u9fff]{2,3})(?=$|[\s，,：:!！])')
_GREETING_ENGLISH = re.compile(
    r'(?i)^(?P<prefix>[ \t]*(?:dear|hi|hello)[ \t]+)'
    r'(?P<name>[a-z]+(?:[ -]+[a-z]+){0,2})(?=$|[\t，,：:!！]|[ ]+(?=[^a-z]))')


def _safe_name(value):
    compact = re.sub(r'[ \t]+','',value)
    return (bool(compact) and not protected_identity(compact)
            and not TECHNICAL_PHRASE.search(compact) and not TECHNICAL_PHRASE.search(value)
            and not privacy._is_generic_name(compact))


def _safe_name_span(match):
    start,end = match.span('name')
    return (_safe_name(match['name']) and not any(
        term.start()<end and term.end()>start for term in TECHNICAL_PHRASE.finditer(match.string)))


def protected_identity(term):
    return (term.casefold() in _TECHNICAL or privacy.PLACEHOLDER.search(term)
            or bool(re.search(r'\d', term)) or bool(_PROTECTED_CODE.fullmatch(term)))


class CandidateIdentityCleaner:
    def __init__(self, terms):
        self.counts = Counter()
        self.terms = {term.casefold() for term in terms if isinstance(term, str)
                      and len(term) >= 2 and not protected_identity(term)}
        self.matcher = privacy.IdentityMatcher(self.terms)
        self.aliases = {term[1:] for term in self.terms
                        if re.fullmatch(r'[\u3400-\u9fff]{3}', term)
                        and term[0] in privacy.CJK_SURNAMES and not protected_identity(term[1:])}

    def preservation(self, original, repaired):
        # A contact's longer organization/display name may itself contain a word
        # such as "工程". Account for those exact contact spans separately; they
        # are not evidence that a process phrase elsewhere was erased.
        def without_contacts(text):
            for _ in range(8):
                pieces, position = [], 0
                for match in privacy.PLACEHOLDER.finditer(text):
                    pieces.extend((self._contacts(text[position:match.start()])[0],match[0]))
                    position = match.end()
                pieces.append(self._contacts(text[position:])[0])
                updated = ''.join(pieces)
                if updated == text:
                    return text
                text = updated
            raise ValueError('contact_audit_did_not_converge')
        before, after = without_contacts(original), without_contacts(repaired)
        return {
            'engineering_expression_mismatches': int(ENGINEERING_EXPRESSION.findall(original) != ENGINEERING_EXPRESSION.findall(repaired)),
            'technical_phrase_mismatches_outside_contacts': int(TECHNICAL_PHRASE.findall(before) != TECHNICAL_PHRASE.findall(after)),
            'technical_tokens_within_contact_redactions': len(TECHNICAL_PHRASE.findall(original))-len(TECHNICAL_PHRASE.findall(before)),
        }

    def _contacts(self,text):
        # Contact initials can also be prefixes of engineering codes (FR-4,
        # IPC-4101) or unit symbols. Preserve the complete engineering span.
        spans = sorted([(m.start(),m.end()) for pattern in (ENGINEERING_EXPRESSION,_ENGINEERING_CODE,privacy.PLACEHOLDER)
                        for m in pattern.finditer(text)])
        guards=[]
        for start,end in spans:
            if guards and start<=guards[-1][1]:
                guards[-1]=(guards[-1][0],max(guards[-1][1],end))
            else:
                guards.append((start,end))
        ends=[end for _,end in guards]
        edits,count=[],0
        for start,end in self.matcher.finditer(text):
            cursor=start
            first=len(edits)
            guard_index=bisect_right(ends,start)
            while guard_index<len(guards) and guards[guard_index][0]<end:
                left,right=guards[guard_index]
                if cursor<left:
                    edits.append((cursor,min(left,end)))
                cursor=max(cursor,min(right,end))
                guard_index+=1
            if cursor<end:
                edits.append((cursor,end))
            count+=len(edits)>first
        pieces,position=[],0
        for start,end in edits:
            pieces.extend((text[position:start],'[身份]'))
            position=end
        pieces.append(text[position:])
        return ''.join(pieces),count

    def _plain(self, text, counts):
        text, count = self._contacts(text)
        counts['archive_contact_matches'] += count

        def label(match):
            name = match['name']
            if not _safe_name_span(match) or name[0] not in privacy.CJK_SURNAMES:
                return match[0]
            counts['labeled_names'] += 1
            return match['label'] + match['sep'] + '[身份]'

        def alias(match):
            if match['name'].casefold() not in self.aliases:
                return match[0]
            counts['contextual_given_names'] += 1
            return match['prefix'] + '[身份]' + match['suffix']

        text = _CONTACT_LABEL.sub(label, text)
        return _ALIAS_CONTEXT.sub(alias, text)

    def _residual_line(self,line,counts):
        # The legacy full-line greeting/signature rules can consume technical
        # prose and numbers in historical attachments. Replace only name spans.
        def before(match):
            name = match['name']
            if name[0] not in privacy.CJK_SURNAMES or not _safe_name_span(match):
                return match[0]
            counts['residual_person_names'] += 1
            return match['prefix']+'[身份]'+match['space']+match['role']
        def after(match):
            name = match['name']
            if name[0] not in privacy.CJK_SURNAMES or not _safe_name_span(match):
                return match[0]
            counts['residual_person_names'] += 1
            return match['role']+match['sep']+'[身份]'
        line = _NAME_BEFORE_ROLE.sub(before,line)
        line = _NAME_AFTER_ROLE.sub(after,line)
        for pattern in (_GREETING_CJK,_GREETING_ENGLISH):
            def greeting(match):
                name = match['name']
                if (not _safe_name_span(match) or name.casefold() in privacy._GENERIC_SALUTATIONS
                        or (privacy.CJK.search(name) and name[0] not in privacy.CJK_SURNAMES)):
                    return match[0]
                counts['residual_person_names'] += 1
                return match['prefix']+'[身份]'
            line = pattern.sub(greeting,line)
        match = privacy._GREETING_NAME_LINE.match(line)
        if match:
            names = [name.strip() for name in re.split(r'[，,、;；]',match['names']) if name.strip()]
            if (1 <= len(names) <= 4 and all(_safe_name(name) and name[0] in privacy.CJK_SURNAMES
                    and re.fullmatch(r'[\u3400-\u9fff]{2,3}',name) for name in names)):
                line = line[:match.start('names')]+'[身份]'+line[match.end('names'):]
                counts['residual_person_names'] += len(names)
        if privacy._ROMAN_CJK_NAME_LINE.fullmatch(line):
            name = re.search(r'[\u3400-\u9fff]{2,4}',line)
            if name and _safe_name(name[0]) and name[0][0] in privacy.CJK_SURNAMES:
                line = (line[:name.start()]+'[身份]'+line[name.end():]
                        if TECHNICAL_PHRASE.search(line) else '[身份]')
                counts['residual_person_names'] += 1
        return line

    def clean(self, text):
        # Existing placeholders must remain byte-for-byte intact. In particular,
        # a contact term must never match inside an anonymization placeholder.
        if not isinstance(text, str):
            raise ValueError('candidate_text_must_be_string')
        for _pass in range(8):
            counts = Counter()
            pieces, position = [], 0
            for match in privacy.PLACEHOLDER.finditer(text):
                pieces.extend((self._plain(text[position:match.start()], counts), match[0]))
                position = match.end()
            pieces.append(self._plain(text[position:], counts))
            updated = ''.join(pieces)
            # Keep the existing residual rules, but restore original line
            # separators: this step must not normalize engineering content.
            lines = re.split(r'(\r\n|\r|\n)', updated)
            updated = ''.join(line if i % 2 else self._residual_line(line, counts)
                              for i, line in enumerate(lines))
            if updated == text:
                return text
            self.counts.update(counts)
            text = updated
        raise ValueError('identity_cleanup_did_not_converge')

    def residuals(self, text):
        result = Counter()
        plain = privacy.PLACEHOLDER.sub(' ', text)
        result['archive_contact_matches'] = self._contacts(text)[1]
        if self.clean(text) != text:
            result['identity_rules_not_stable'] += 1
        # These rules audit already sanitized data; re-running broad numerical
        # or business-field substitutions here would erase process values.
        for name, pattern in (('email', privacy.EMAIL), ('obfuscated_email', privacy.OBFUSCATED_EMAIL),
                              ('url', privacy.URL), ('path', privacy.PATH),
                              ('credential', privacy.CREDENTIAL)):
            result[name] = sum(1 for _ in pattern.finditer(plain))
        return {key: count for key, count in result.items() if count}
