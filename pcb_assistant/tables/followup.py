"""Bounded conversation locators select catalog records, never supply evidence."""
import re

from pcb_assistant.materials.selection import is_selection_question
from pcb_assistant.tables.fields import field_followup_requested, focused_fields

DOC_PATTERN=r'(?:MAIL|CASE|KNOWLEDGE|ATTACHMENT|PUBLIC)_[0-9a-f]{24,32}'
_LOCATOR=re.compile(r'核对定位：来源 ('+DOC_PATTERN+r')；原表行(\d+)；区块列(\d+)–(\d+)。')
_RESET=re.compile(r'新问题|新問題|另一个问题|另一個問題|另一批|新批次|重新开始|重新開始|换个话题|換個話題|\bnew (?:case|topic|batch)\b',re.I)
_FOCUS=re.compile(r'原表行\s*\d+|第\s*\d+\s*行|第\s*[一二三四五六七八九十\d]+\s*份|'
                  r'左(?:侧|側|区块|區塊)|右(?:侧|側|区块|區塊)|这(?:一)?行|這(?:一)?行|该行|該行|这份|這份|同一来源|同一來源')


def is_table_scope_request(question, history=()):
    if _RESET.search(question):
        return False
    explicit=bool(re.search(DOC_PATTERN,question) and re.search(r'原表|区块|區塊|表头|表頭|叠构|疊構',question))
    previous=next((r['content'] for r in reversed(history[-8:]) if r.get('role')=='assistant'), '')
    references=bool(_LOCATOR.search(previous))
    continuation=bool(_FOCUS.search(question) or re.search(r'继续核对|繼續核對|继续看|繼續看',question)
                      or field_followup_requested(question) or focused_fields(question,previous))
    # A fresh full selection question begins a fresh search unless it names a source.
    return explicit or (references and continuation and not is_selection_question(question))


def resolve_table_scope(question, history):
    if not is_table_scope_request(question,history):
        return None
    previous=next((r['content'] for r in reversed(history[-8:]) if r.get('role')=='assistant'), '')
    locators=[]
    for m in _LOCATOR.finditer(previous):
        item={'doc_id':m[1],'row':int(m[2]),'first_column':int(m[3]),'last_column':int(m[4])}
        if item not in locators:
            locators.append(item)
    ids=list(dict.fromkeys(re.findall(DOC_PATTERN,question)))
    ordinal=re.search(r'第\s*([一二三四五六七八九十]|\d+)\s*份',question)
    selected=None
    if len(ids)>1:
        return {'status':'ambiguous_source','candidates':locators}
    if ids:
        matches=[x for x in locators if x['doc_id']==ids[0]]
        selected=matches[0] if len(matches)==1 else {'doc_id':ids[0]}
    elif ordinal:
        number=int(ordinal[1]) if ordinal[1].isdigit() else '一二三四五六七八九十'.index(ordinal[1])+1
        if 1<=number<=len(locators):
            selected=locators[number-1]
        else:
            return {'status':'source_ordinal_out_of_range','candidates':locators}
    elif len(locators)==1:
        selected=locators[0]
    if not selected:
        return {'status':'ambiguous_source','candidates':locators}
    result={'status':'ready',**selected}
    fields=focused_fields(question,previous)
    if fields:
        result['fields']=list(fields)
    # The source ordinal must not be mistaken for a row selector.
    row_match=re.search(r'原表行\s*(\d+)|第\s*(\d+)\s*行',question)
    if row_match:
        result['row']=int(row_match[1] or row_match[2])
    left=bool(re.search(r'左(?:侧|側|区块|區塊)',question))
    right=bool(re.search(r'右(?:侧|側|区块|區塊)',question))
    if left and right:
        return {'status':'ambiguous_block','candidates':locators}
    if left or right:
        result['side']='left' if left else 'right'
        result.pop('first_column',None);result.pop('last_column',None)
    if not result.get('row'):
        result['status']='row_required'
    return result


def scope_locator_lines(checks):
    return ['核对定位：来源 {}；原表行{}；区块列{}–{}。'.format(
        c['doc_id'],c['data_row'],c['first_column'],c['last_column']) for c in checks]
