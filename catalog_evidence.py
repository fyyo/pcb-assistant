"""Read-only, source-grounded evidence packages over the unified catalog."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from catalog_common import CatalogError, DEFAULT_CATALOG, connect_readonly
from catalog_common import json_text as _canonical_json
import knowledge_catalog as catalog
from pcb_knowledge import (
    QUERY_CONTEXT_GROUPS, explanation_quality, field_coverage,
    is_explanation_question, retrieval_query,
)
from pcb_selection import STACKUP, has_alias, is_selection_question, selection_conditions, selection_quality
from pcb_material_concepts import (
    definition_keys, material_concepts, material_paragraph_window, material_ranking_keys,
)
from pcb_table_context import (
    table_excerpt_pair, table_layout_pair, source_location, is_table_question, table_row_context,
    parallel_column_groups, parallel_excerpt_spans, scoped_table_spans,
)


EVIDENCE_SCHEMA = "catalog-evidence-v2"
FINGERPRINT_ALGORITHM = "sha256-canonical-json-v1"
MAX_EVIDENCE_LIMIT = 50
MAX_EVIDENCE_OFFSET = 400
MIN_CANDIDATE_WINDOW = 200
MAX_CANDIDATE_WINDOW = 400
RANKING_POOL_PER_GROUP = 500
STACKUP_TABLE_POOL_LIMIT = 40
EXCERPT_OFFSET_UNIT = "unicode_codepoints"
MAX_EXCERPTS_PER_SOURCE = 2
MAX_EXCERPT_CHARS = 360
_EXCERPT_LEFT_CONTEXT = 120
_EXCERPT_RIGHT_CONTEXT = 240
_EXCERPT_BOUNDARIES = "。！？!?；;\n\r"
_TYPE_PRIORITY = {
    "MAIL": 0,
    "ATTACHMENT": 1,
    "PUBLIC": 2,
    "CASE": 3,
    "KNOWLEDGE": 4,
}


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= MAX_EVIDENCE_LIMIT:
        raise CatalogError("invalid_evidence_limit")


def _validate_offset(offset):
    if type(offset) is not int or not 0 <= offset <= MAX_EVIDENCE_OFFSET:
        raise CatalogError("invalid_evidence_offset")


def _candidate_limit(limit, offset):
    return max(MIN_CANDIDATE_WINDOW,
               min(MAX_CANDIDATE_WINDOW, (offset + limit) * 8))


def fingerprint_result(result):
    """Return the deterministic SHA-256 fingerprint of an evidence response."""
    if not isinstance(result, dict):
        raise CatalogError("invalid_evidence_result")
    payload = {
        key: value for key, value in result.items()
        if key not in {"fingerprint_algorithm", "result_fingerprint"}
    }
    try:
        canonical = _canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_evidence_result") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finalize_result(result):
    result["fingerprint_algorithm"] = FINGERPRINT_ALGORITHM
    result["result_fingerprint"] = fingerprint_result(result)
    return result


def export_evidence(result, destination, *, overwrite=False, catalog_directory=None):
    """Write one verified evidence response to a user-selected local JSON file.

    The export is a local side effect only. It never writes into the Catalog
    directory, never modifies source assets, and requires no confirmation step.
    """
    if not isinstance(result, dict):
        raise CatalogError("invalid_evidence_result")
    if type(overwrite) is not bool:
        raise CatalogError("invalid_evidence_export_overwrite")
    computed = fingerprint_result(result)
    if (result.get("fingerprint_algorithm") != FINGERPRINT_ALGORITHM
            or result.get("result_fingerprint") != computed):
        raise CatalogError("invalid_evidence_fingerprint")

    try:
        target = Path(destination).expanduser().resolve(strict=False)
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_evidence_export_path") from exc
    if target.suffix.lower() != ".json":
        raise CatalogError("evidence_export_requires_json")
    if catalog_directory is not None:
        catalog_root = Path(catalog_directory).expanduser().resolve(strict=False)
        if target == catalog_root or target.is_relative_to(catalog_root):
            raise CatalogError("evidence_export_inside_catalog_forbidden")
    if not target.parent.is_dir():
        raise CatalogError("evidence_export_parent_missing")

    try:
        payload = (_canonical_json(result, pretty=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalogError("invalid_evidence_result") from exc
    try:
        with target.open("wb" if overwrite else "xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise CatalogError("evidence_export_exists") from exc
    except OSError as exc:
        raise CatalogError("evidence_export_failed") from exc

    return {
        "status": "exported",
        "path": str(target),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "result_fingerprint": computed,
        "read_only": True,
        "human_confirmation_required": False,
        "generated_by_model": False,
        "retrieval_only": True,
        "engineering_conclusion_supported": False,
    }


def _present_terms(text, terms):
    if not isinstance(text, str):
        return []
    folded = text.casefold()
    return [term for term in terms if term and term.casefold() in folded]


def _line_number(text, offset):
    return text.count("\n", 0, max(0, offset)) + 1


def _excerpt_window(text, hit_start, hit_end):
    """Expand a literal hit to a bounded, sentence-oriented excerpt window."""
    lower = max(0, hit_start - _EXCERPT_LEFT_CONTEXT)
    left_candidates = [text.rfind(char, lower, hit_start) for char in _EXCERPT_BOUNDARIES]
    left_boundary = max(left_candidates, default=-1)
    start = left_boundary + 1 if left_boundary >= lower else max(0, hit_start - 80)

    upper = min(len(text), hit_end + _EXCERPT_RIGHT_CONTEXT)
    right_candidates = [text.find(char, hit_end, upper) for char in _EXCERPT_BOUNDARIES]
    right_candidates = [position for position in right_candidates if position >= 0]
    right_boundary = min(right_candidates, default=-1)
    end = right_boundary + 1 if right_boundary >= 0 else min(len(text), hit_end + 180)

    if end - start > MAX_EXCERPT_CHARS:
        start = max(0, hit_start - 80)
        end = min(len(text), hit_end + 180)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _excerpts_for_text(text, terms, content_id, *, ranking_terms=(), explanation=False,
                       selection=False, conditions=(), table_scope=None):
    """Return deterministic literal excerpts without interpreting the source."""
    if not isinstance(text, str) or not text.strip():
        return []
    material_keys = material_ranking_keys(ranking_terms) if explanation else ()
    hits = []
    for term in terms:
        if not term:
            continue
        try:
            matches = re.finditer(re.escape(term), text, flags=re.IGNORECASE)
        except re.error:
            continue
        for match in matches:
            hits.append((match.start(), match.end()))
    if not hits and not table_scope:
        return []

    candidates = []
    seen = set()
    for hit_start, hit_end in hits:
        start, end = _excerpt_window(text, hit_start, hit_end)
        if material_keys:
            paragraph = material_paragraph_window(text, hit_start, hit_end, MAX_EXCERPT_CHARS)
            if paragraph and explanation_quality(text[paragraph[0]:paragraph[1]], ranking_terms)[1]:
                start, end = paragraph
                while start < end and text[start].isspace():
                    start += 1
                while end > start and text[end - 1].isspace():
                    end -= 1
        context_groups = [group for group in ranking_terms if group in QUERY_CONTEXT_GROUPS]
        if context_groups:
            # Keep an adjacent product condition with its fill/defect statement.
            # Preserve one contiguous source slice and the original bounds cap.
            line_start = text.rfind('\n', 0, start) + 1
            previous = text.rfind('\n', 0, max(0, line_start - 1)) + 1
            following = text.find('\n', end + 1)
            if following < 0:
                following = len(text)
            for candidate_start, candidate_end in ((previous, end), (start, following)):
                if candidate_end - candidate_start > MAX_EXCERPT_CHARS:
                    continue
                current = text[start:end].casefold()
                expanded = text[candidate_start:candidate_end].casefold()
                if any(not any(term in current for term in group)
                       and any(term in expanded for term in group) for group in context_groups):
                    start, end = candidate_start, candidate_end
                    while start < end and text[start].isspace():
                        start += 1
                    while end > start and text[end - 1].isspace():
                        end -= 1
        if start >= end or (start, end) in seen:
            continue
        seen.add((start, end))
        excerpt_text = text[start:end]
        if material_keys and not set(material_concepts(excerpt_text)) & set(material_keys):
            continue
        matched_terms = _present_terms(excerpt_text, terms)
        if not matched_terms:
            continue
        candidates.append((start, end, excerpt_text, matched_terms))
        if selection:
            # Keep adjacent headings/values in one source slice, without joining
            # disconnected table cells or inventing a column correspondence.
            lower = max(0, hit_start - 110)
            upper = min(len(text), lower + MAX_EXCERPT_CHARS)
            expanded_start = text.find('\n', lower, hit_start) + 1 if lower else 0
            if expanded_start <= 0:
                expanded_start = lower
            expanded_end = text.rfind('\n', hit_end, upper)
            if expanded_end < hit_end:
                expanded_end = upper
            while expanded_start < expanded_end and text[expanded_start].isspace():
                expanded_start += 1
            while expanded_end > expanded_start and text[expanded_end-1].isspace():
                expanded_end -= 1
            if (expanded_start <= hit_start and expanded_end >= hit_end
                    and (expanded_start, expanded_end) not in seen):
                seen.add((expanded_start, expanded_end))
                expanded = text[expanded_start:expanded_end]
                candidates.append((expanded_start, expanded_end, expanded,
                                   _present_terms(expanded, terms)))

    # For explanation requests, a complete statement outranks a repeated heading.
    def rank(item):
        body = item[2].casefold()
        coverage = sum(any(term in body for term in group) for group in ranking_terms)
        topics, complete = explanation_quality(body, ranking_terms) if explanation else (0, 0)
        choice = selection_quality(item[2], conditions) if selection else ()
        return (*(-v for v in choice), -topics, -complete, -coverage,
                -len(item[3]), item[0], item[1])
    candidates.sort(key=rank)
    selected = []
    remaining = list(candidates)
    covered_definitions = set()
    while remaining:
        if material_keys:
            remaining.sort(key=lambda item: (
                -len(set(definition_keys(item[2], material_keys)) - covered_definitions), *rank(item)))
        candidate = remaining.pop(0)
        start, end = candidate[0], candidate[1]
        if any(not (end <= chosen[0] or start >= chosen[1]) for chosen in selected):
            continue
        selected.append(candidate)
        if material_keys:
            covered_definitions.update(definition_keys(candidate[2], material_keys))
        if len(selected) >= MAX_EXCERPTS_PER_SOURCE:
            break
    selected.sort(key=lambda item: (item[0], item[1]))

    table_pair = table_excerpt_pair(text, terms, conditions, MAX_EXCERPT_CHARS) if selection else None
    parallel_spans = parallel_excerpt_spans(text,terms,conditions,MAX_EXCERPT_CHARS) if selection else None
    layout_pair = (table_layout_pair(text, terms, MAX_EXCERPT_CHARS, conditions)
                   if selection and not table_pair and not parallel_spans else None)
    chosen_pair = table_pair or layout_pair
    if chosen_pair:
        spans, _ = chosen_pair
        selected = [(start, end, text[start:end], _present_terms(text[start:end], terms))
                    for start, end in spans]
    elif parallel_spans:
        selected = [(start,end,text[start:end],_present_terms(text[start:end],terms))
                    for start,end in parallel_spans]
    if table_scope:
        spans,_ = scoped_table_spans(text,table_scope['row'],MAX_EXCERPT_CHARS)
        selected=[(start,end,text[start:end],_present_terms(text[start:end],terms)) for start,end in spans]
        table_pair=layout_pair=None

    parallel = parallel_column_groups(text) if selection else None
    if table_scope:
        parallel=None
        if selected:
            start,end=selected[0][:2]
            parallel=parallel_column_groups(text[start:end])
            if parallel:
                parallel['header_start']+=start
                parallel['header_end']+=start
    if parallel:
        lo, hi = parallel['header_start'], parallel['header_end']
        if hi-lo <= MAX_EXCERPT_CHARS and not any(s<=lo and e>=hi for s,e,_,_ in selected):
            # Return the actual bounded header alongside a hit; never reconstruct it.
            hits = [item for item in selected if item[1]<=lo or item[0]>=hi]
            if hits and not chosen_pair and not table_scope:
                selected = [(lo,hi,text[lo:hi],_present_terms(text[lo:hi],terms)),hits[0]]
                selected.sort(key=lambda item:item[0])

    excerpts = []
    for start, end, excerpt_text, matched_terms in selected:
        digest = hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest()
        identity = f"{content_id}\0{start}\0{end}\0{excerpt_text}"
        excerpt_id = "EXC_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        excerpts.append({
            "excerpt_id": excerpt_id,
            "text": excerpt_text,
            "start": start,
            "end": end,
            "offset_unit": EXCERPT_OFFSET_UNIT,
            "line_start": _line_number(text, start),
            "line_end": _line_number(text, max(start, end - 1)),
            "matched_terms": matched_terms,
            "sha256": digest,
        })
        if selection:
            if parallel and start<=parallel['header_start'] and end>=parallel['header_end']:
                excerpts[-1]['parallel_table_groups'] = parallel
            context = table_row_context(text,start,end)
            if context:
                excerpts[-1]['table_row_roles'] = context
    if table_pair:
        _, record = table_pair
        for excerpt in excerpts:
            excerpt['table_context'] = record
    if layout_pair:
        _, record = layout_pair
        for excerpt in excerpts:
            excerpt['table_layout_context'] = record
    return excerpts


def _excerpt_status(text, excerpts):
    """Describe why a source does or does not have a literal excerpt."""
    if not isinstance(text, str) or not text.strip():
        return "no_body_text"
    if excerpts:
        return "excerpts_found"
    return "no_literal_excerpt_match"


def _normalize_ranking_terms(ranking_terms):
    """Validate optional concept groups used for deterministic reranking."""
    if ranking_terms is None:
        return ()
    if isinstance(ranking_terms, str) or not isinstance(ranking_terms, (list, tuple)):
        raise CatalogError("invalid_evidence_ranking_terms")
    groups = []
    seen_groups = set()
    total_terms = 0
    for group in ranking_terms:
        if isinstance(group, str) or not isinstance(group, (list, tuple)):
            raise CatalogError("invalid_evidence_ranking_terms")
        terms = []
        for term in group:
            if not isinstance(term, str):
                raise CatalogError("invalid_evidence_ranking_terms")
            term = term.strip().casefold()
            if not term or len(term) > 40:
                raise CatalogError("invalid_evidence_ranking_terms")
            if term not in terms:
                terms.append(term)
        if not terms:
            continue
        key = tuple(terms)
        if key in seen_groups:
            continue
        seen_groups.add(key)
        groups.append(key)
        total_terms += len(terms)
    if len(groups) > 12 or total_terms > 80:
        raise CatalogError("invalid_evidence_ranking_terms")
    return tuple(groups)


def _ranking_expression(ranking_terms):
    """Build a literal concept-coverage score without changing the FTS match."""
    if not ranking_terms:
        return "0", [], "bm25"
    clauses = []
    parameters = []
    for group in ranking_terms:
        alternatives = []
        for term in group:
            alternatives.append("instr(lower(COALESCE(d.title,'')),?)>0")
            alternatives.append(
                "instr(lower(COALESCE(rank_content.normalized_text,"
                "rank_content.original_text,'')),?)>0")
            parameters.extend((term, term))
        clauses.append("(" + " OR ".join(alternatives) + ")")
    expression = " + ".join(
        f"CASE WHEN {clause} THEN 1 ELSE 0 END" for clause in clauses)
    return expression, parameters, "core_term_coverage_then_bm25"


def _pooled_candidate_sql(scope, ranking_terms, ranking_expression,
                          ranking_parameters, candidate_limit):
    """Narrow each concept group by BM25 before applying coverage reranking.

    Wide questions can match most of the catalog. Ranking every match with
    literal ``instr`` coverage is unnecessarily expensive, so each group first
    keeps its top BM25 documents and the union is reranked by the existing
    concept-coverage expression. Callers retain the full-match query as a
    correctness fallback when a group expression produces no candidates.
    """
    branch = f"""SELECT doc_id FROM (
        SELECT d.doc_id,{scope['score']} AS score
        FROM documents d {scope['join']}
        WHERE {scope['where']} AND document_search MATCH ?
        ORDER BY score,d.doc_id
        LIMIT ?)"""
    branches = []
    parameters = []
    for group in ranking_terms:
        expression = " OR ".join(
            '"' + term.replace('"', '""') + '"' for term in group)
        branches.append(branch)
        parameters.extend((
            *scope["parameters"], expression, RANKING_POOL_PER_GROUP))
    ranking_join = (
        " LEFT JOIN contents rank_content ON rank_content.content_id=d.content_id")
    sql = f"""WITH pooled(doc_id) AS (
        {' UNION '.join(branches)}
    )
    SELECT d.doc_id,d.type,d.source_type,d.title,d.source_version,
               d.source_path,d.locator_json,d.evidence_status,d.independently_verified,
               d.content_status,d.format,d.extraction_method,d.content_id,
               {scope['score']} AS score,
               {ranking_expression} AS ranking_coverage
        FROM documents d
        JOIN pooled ON pooled.doc_id=d.doc_id
        {scope['join']}{ranking_join}
        WHERE {scope['where']}
        ORDER BY ranking_coverage DESC,score,d.doc_id
        LIMIT ?"""
    return sql, [
        *parameters, *ranking_parameters, *scope["parameters"],
        candidate_limit + 1,
    ]


def _stackup_table_candidates(connection, scope, ranking_expression, ranking_parameters, conditions):
    """Reserve a bounded indexed pool for explicitly requested stackup sheets.

    Existing tokenization stores a weave/count cell such as 2116x2 as one
    token. A numeric prefix is only a retrieval aid, never a verified count or
    material match. Column/version semantics still require returned evidence.
    """
    glass = [c['value'] for c in conditions if c['kind']=='glass']
    if not glass or 'document_search' not in scope['join']:
        return [], False
    weave = ' OR '.join('"'+value+'"*' for value in glass)
    expression = '('+weave+') AND ("层别" OR "層別" OR "layer")'
    sql = f"""SELECT d.doc_id,d.type,d.source_type,d.title,d.source_version,
            d.source_path,d.locator_json,d.evidence_status,d.independently_verified,
            d.content_status,d.format,d.extraction_method,d.content_id,
            {scope['score']} AS score,{ranking_expression} AS ranking_coverage
        FROM documents d {scope['join']}
        LEFT JOIN contents rank_content ON rank_content.content_id=d.content_id
        WHERE {scope['where']} AND d.source_type='attachment'
          AND d.format IN ('xls','xlsx') AND document_search MATCH ?
        ORDER BY score,d.doc_id LIMIT ?"""
    rows=connection.execute(sql,[*ranking_parameters,*scope['parameters'],expression,
                                 STACKUP_TABLE_POOL_LIMIT+1]).fetchall()
    return rows[:STACKUP_TABLE_POOL_LIMIT], len(rows)>STACKUP_TABLE_POOL_LIMIT


def _base_result(query, scope, *, limit, offset, candidate_limit,
                 ranking_terms=(), ranking_basis="bm25", retrieval_text=None):
    result = {
        "schema": EVIDENCE_SCHEMA,
        "status": "no_evidence",
        "query": query,
        "retrieval_query": query if retrieval_text is None else retrieval_text,
        "match_terms": scope["match_terms"],
        "filters": scope["filters"],
        "limit": limit,
        "offset": offset,
        "returned": 0,
        "page_source_count": 0,
        "next_offset": None,
        "has_more_in_window": False,
        "pagination_scope": "bounded_candidate_window_not_full_catalog",
        "candidate_window_limit": candidate_limit,
        "candidate_representations": 0,
        "candidate_window_truncated": False,
        "source_count_in_window": 0,
        "source_count_scope": "bounded_candidate_window_not_full_catalog",
        "excerpt_policy": {
            "max_excerpts_per_source": MAX_EXCERPTS_PER_SOURCE,
            "max_characters": MAX_EXCERPT_CHARS,
            "offset_unit": EXCERPT_OFFSET_UNIT,
            "selection_basis": "literal_match_terms_sentence_window",
        },
        "excerpt_count": 0,
        "sources_with_excerpts": 0,
        "read_only": True,
        "generated_by_model": False,
        "human_confirmation_required": False,
        "source_text_is_untrusted_data": True,
        "retrieval_only": True,
        "engineering_conclusion_supported": False,
        "results": [],
    }
    if ranking_terms:
        result["ranking"] = {
            "basis": ranking_basis,
            "term_groups": [list(group) for group in ranking_terms],
        }
    return result


def _load_primary_mappings(connection, doc_ids):
    placeholders = ",".join("?" for _ in doc_ids)
    rows = connection.execute(
        f"""SELECT doc_id,source_id,source_version,source_path,locator_json
            FROM source_map WHERE role='primary' AND doc_id IN ({placeholders})""",
        doc_ids)
    return {row["doc_id"]: dict(row) for row in rows}


def _load_texts(connection, content_ids):
    placeholders = ",".join("?" for _ in content_ids)
    rows = connection.execute(
        f"""SELECT content_id,original_text,normalized_text
            FROM contents WHERE content_id IN ({placeholders})""",
        content_ids)
    texts = {}
    for row in rows:
        normalized = row["normalized_text"] is not None
        selected_text = row["normalized_text"] if normalized else row["original_text"]
        texts[row["content_id"]] = {
            "text": selected_text,
            "text_variant": "normalized" if normalized else "original",
            "text_sha256": hashlib.sha256(selected_text.encode("utf-8")).hexdigest(),
        }
    return texts


def _page_field_texts(result):
    """Title plus exact excerpt text for every excerpt in the returned page."""
    return [row.get("title", "") + "\n" + excerpt["text"]
            for row in result.get("results", ())
            for excerpt in row.get("excerpts", ())]


def _with_field_coverage(result):
    """Attach the shared field coverage, then finalize the fingerprint."""
    result["evidence_field_coverage"] = field_coverage(_page_field_texts(result))
    return _finalize_result(result)


def evidence(directory: Path = DEFAULT_CATALOG, query: str = "", *, doc_type=None,
             source_type=None, tags=(), limit=10, offset=0, source_version=None,
             content_status=None, ranking_terms=(), table_scope=None) -> dict:
    """Return a bounded, deterministic package of retrieved catalog excerpts.

    The package is deliberately retrieval-only. It does not synthesize an
    engineering conclusion, alter the catalog, or require human confirmation.
    Counts describe the bounded candidate window, not the full catalog.
    """
    if not isinstance(query, str) or not query.strip():
        raise CatalogError("query_required")
    _validate_limit(limit)
    _validate_offset(offset)
    query = query.strip()
    explanation = is_explanation_question(query)
    selection = is_selection_question(query)
    table_focus = selection and is_table_question(query)
    conditions = selection_conditions(query) if selection else ()
    retrieval_text = retrieval_query(query)
    ranking_terms = _normalize_ranking_terms(ranking_terms)
    material_keys = material_ranking_keys(ranking_terms) if explanation else ()
    scope = catalog._search_scope(
        '' if table_scope else retrieval_text, doc_type=doc_type, source_type=source_type, tags=tags,
        query_mode="fulltext", source_version=source_version,
        content_status=content_status)
    candidate_limit = _candidate_limit(limit, offset)
    ranking_expression, ranking_parameters, ranking_basis = _ranking_expression(
        ranking_terms)
    if table_scope:
        ranking_expression,ranking_parameters='0',[]
        if table_scope.get('status')=='ready':
            scope['where']+=' AND d.doc_id=?'
            scope['parameters'].append(table_scope['doc_id'])
        else:
            scope['where']+=' AND 0=1'
    ranking_join = (
        " LEFT JOIN contents rank_content ON rank_content.content_id=d.content_id"
        if ranking_terms else "")
    result = _base_result(
        query, scope, limit=limit, offset=offset,
        candidate_limit=candidate_limit, ranking_terms=ranking_terms,
        ranking_basis=ranking_basis, retrieval_text=retrieval_text)
    if table_scope:
        result['table_followup_scope']=dict(table_scope)
    if explanation and ranking_terms:
        result['ranking']['basis'] = 'topical_explanatory_excerpt_then_coverage_bm25'
    if selection and ranking_terms:
        result['ranking']['basis'] = 'selection_conditions_and_excerpt_context_then_coverage_bm25'
        if table_focus:
            result['ranking']['basis'] = 'explicit_table_context_then_selection_conditions_and_coverage_bm25'
    fallback_candidate_sql = f"""SELECT d.doc_id,d.type,d.source_type,d.title,d.source_version,
               d.source_path,d.locator_json,d.evidence_status,d.independently_verified,
               d.content_status,d.format,d.extraction_method,d.content_id,
               {scope['score']} AS score,
               {ranking_expression} AS ranking_coverage
        FROM documents d {scope['join']}{ranking_join}
        WHERE {scope['where']}
        ORDER BY ranking_coverage DESC,score,d.doc_id
        LIMIT ?"""
    fallback_parameters = [
        *ranking_parameters, *scope["parameters"], candidate_limit + 1]
    use_ranking_pool = bool(
        ranking_terms and "document_search" in scope["join"])
    if use_ranking_pool:
        candidate_sql, candidate_parameters = _pooled_candidate_sql(
            scope, ranking_terms, ranking_expression, ranking_parameters,
            candidate_limit)
    else:
        candidate_sql, candidate_parameters = (
            fallback_candidate_sql, fallback_parameters)

    with closing(connect_readonly(directory)) as connection:
        fetched = connection.execute(candidate_sql, candidate_parameters).fetchall()
        if use_ranking_pool and not fetched:
            fetched = connection.execute(
                fallback_candidate_sql, fallback_parameters).fetchall()
        result["candidate_window_truncated"] = len(fetched) > candidate_limit
        candidates = fetched[:candidate_limit]
        if not table_scope and table_focus and has_alias(query, STACKUP):
            table_pool, truncated = _stackup_table_candidates(
                connection,scope,ranking_expression,ranking_parameters,conditions)
            ids={r['doc_id'] for r in table_pool}
            merged=[*table_pool,*(r for r in candidates if r['doc_id'] not in ids)]
            result['candidate_pools'] = {
                'stackup_table':{'basis':'indexed_weave_prefix_and_layer_heading_in_spreadsheet',
                                 'limit':STACKUP_TABLE_POOL_LIMIT,'returned':len(table_pool),
                                 'truncated':truncated,'engineering_conclusion_supported':False}}
            result['candidate_window_truncated'] |= truncated or len(merged)>candidate_limit
            candidates=merged[:candidate_limit]
        result["candidate_representations"] = len(candidates)
        if not candidates:
            if table_scope and table_scope.get('status')=='ready':
                result['table_followup_scope']['status']='source_missing_or_filtered'
            result["no_evidence_reason"] = "no_matching_catalog_records"
            return _with_field_coverage(result)

        excerpt_terms = list(dict.fromkeys([
            *scope["match_terms"], *(term for group in ranking_terms for term in group)]))
        explanation_scores = {}
        selection_scores = {}
        table_scores = {}
        excerpt_cache = {}
        if explanation or selection:
            texts = _load_texts(connection, list({row['content_id'] for row in candidates}))
            for content_id, info in texts.items():
                excerpts = _excerpts_for_text(info.get('text', ''), excerpt_terms, content_id,
                                              ranking_terms=ranking_terms, explanation=explanation,
                                              selection=selection, conditions=conditions, table_scope=table_scope)
                excerpt_cache[content_id] = excerpts
                explanation_scores[content_id] = max(
                    (explanation_quality(excerpt['text'], ranking_terms) for excerpt in excerpts),
                    default=(0, 0))
                if material_keys:
                    definitions = {key for excerpt in excerpts
                                   for key in definition_keys(excerpt['text'], material_keys)}
                    mentions = {key for excerpt in excerpts
                                for key in material_concepts(excerpt['text']) if key in material_keys}
                    explanation_scores[content_id] = (
                        len(definitions) * (len(material_keys) + 1) + len(mentions), int(bool(definitions)))
                if selection:
                    selection_scores[content_id] = max(
                        (selection_quality(e['text'], conditions) for e in excerpts), default=(0,)*7)
                    table_scores[content_id] = int(any(
                        e.get('table_context') or e.get('table_layout_context')
                        or e.get('parallel_table_groups') for e in excerpts))

        def candidate_order(row):
            quality = explanation_scores.get(row['content_id'], (0, 0))
            choice = selection_scores.get(row['content_id'], ())
            return ((-table_scores.get(row['content_id'],0),) if table_focus else ()) + (
                    *(-v for v in choice), -quality[0], -quality[1], -int(row['ranking_coverage']),
                    float(row['score']))

        mappings = _load_primary_mappings(
            connection, [row["doc_id"] for row in candidates])
        clusters = {}
        for row in candidates:
            mapping = mappings.get(row["doc_id"], {})
            source_key = mapping.get("source_id") or row["doc_id"]
            clusters.setdefault(source_key, []).append((row, mapping))
        for items in clusters.values():
            items.sort(key=lambda item: (
                *candidate_order(item[0]),
                _TYPE_PRIORITY.get(item[0]["type"], 5),
                item[0]["doc_id"],
            ))

        ordered = sorted(clusters.items(), key=lambda item: (
            *candidate_order(item[1][0][0]), item[0]))
        if selection:
            # Merge only identical complete catalog bodies, retaining every
            # representation/locator; similar excerpts are not duplicate sources.
            unique = {}
            for source_key, items in ordered:
                content_id = items[0][0]['content_id']
                if content_id in unique:
                    unique[content_id][1].extend(items)
                else:
                    unique[content_id] = (source_key, list(items))
            result['deduplication'] = {
                'basis':'identical_complete_content_id_within_candidate_window',
                'source_groups_before':len(ordered), 'source_groups_after':len(unique),
                'merged_source_groups':len(ordered)-len(unique),
            }
            ordered = list(unique.values())
        result["source_count_in_window"] = len(ordered)
        if offset >= len(ordered):
            result["status"] = "page_out_of_range"
            return _with_field_coverage(result)
        selected = ordered[offset:offset + limit]
        content_ids = [items[0][0]["content_id"] for _, items in selected]
        if explanation or selection:
            texts = {key: texts[key] for key in content_ids if key in texts}
            excerpt_cache = {key: excerpt_cache[key] for key in content_ids if key in excerpt_cache}
        else:
            texts = _load_texts(connection, content_ids)

    rows = []
    for source_key, items in selected:
        representative, mapping = items[0]
        text_info = texts.get(representative["content_id"], {})
        text = text_info.get("text", "")
        preview = catalog._context_preview(text,
                                           scope["match_terms"])
        content_id = representative["content_id"]
        if content_id in excerpt_cache:
            # Locator metadata belongs to a source representation, not its body.
            # Keep cached excerpts isolated when several sources share content.
            excerpts = deepcopy(excerpt_cache[content_id])
        else:
            excerpts = _excerpts_for_text(
                text, excerpt_terms, content_id, ranking_terms=ranking_terms,
                explanation=explanation, selection=selection, conditions=conditions,
                table_scope=table_scope)
        if table_scope:
            _,status=scoped_table_spans(text,table_scope['row'],MAX_EXCERPT_CHARS)
            result['table_followup_scope']['status']=status
        if selection:
            locator = json.loads(mapping.get('locator_json') or representative['locator_json'])
            for excerpt in excerpts:
                excerpt['source_location'] = source_location(
                    representative['title'], locator, text, excerpt['start'], excerpt['end'])
        excerpt_status = _excerpt_status(text, excerpts)
        representations = []
        for item, item_mapping in items:
            representations.append({
                "doc_id": item["doc_id"],
                **({'source_id':item_mapping.get('source_id') or item['doc_id']} if selection else {}),
                "type": item["type"],
                "title": item["title"],
                "source_version": item_mapping.get("source_version") or item["source_version"],
                "source_path": item_mapping.get("source_path") or item["source_path"],
                "locator": json.loads(item_mapping.get("locator_json") or item["locator_json"]),
                "evidence_status": item["evidence_status"],
                "independently_verified": bool(item["independently_verified"]),
                "content_status": item["content_status"],
                "retrieval_score": round(float(item["score"]), 6),
                "is_representative": item is representative,
            })
        rows.append({
            "evidence_key": "source:" + source_key,
            "source_id": mapping.get("source_id") or source_key,
            "doc_id": representative["doc_id"],
            "type": representative["type"],
            "title": representative["title"],
            "source_type": representative["source_type"],
            "source_version": representative["source_version"],
            "source_path": representative["source_path"],
            "content_status": representative["content_status"],
            "evidence_status": representative["evidence_status"],
            "independently_verified": bool(representative["independently_verified"]),
            "preview": preview,
            "matched_terms": _present_terms(representative["title"] + " " + preview,
                                             scope["match_terms"]),
            "excerpt_status": excerpt_status,
            "excerpt_count": len(excerpts),
            "excerpts": excerpts,
            "excerpt_text_variant": text_info.get("text_variant"),
            "excerpt_text_sha256": text_info.get("text_sha256"),
            "retrieval_score": round(float(representative["score"]), 6),
            "representation_count_in_window": len(items),
            "representations": representations,
            "citation": {
                "doc_id": representative["doc_id"],
                "source_id": mapping.get("source_id") or source_key,
                "source_version": mapping.get("source_version") or representative["source_version"],
                "source_type": representative["source_type"],
                "source_path": mapping.get("source_path") or representative["source_path"],
                "locator": json.loads(mapping.get("locator_json") or representative["locator_json"]),
            },
        })
        if ranking_terms:
            rows[-1]["ranking_coverage"] = int(representative["ranking_coverage"])
            rows[-1]["ranking_basis"] = (
                "core_term_coverage_then_fts5_bm25_"
                "best_representation_then_source_id")
        if explanation:
            rows[-1]['explanation_quality'] = {
                'complete_statement': bool(explanation_scores.get(representative['content_id'], (0, 0))[1]),
                'basis': 'bounded_topical_excerpt_with_explanatory_marker',
            }
            if ranking_terms:
                rows[-1]['ranking_basis'] = 'topical_explanatory_excerpt_then_coverage_bm25'
        if selection and ranking_terms:
            rows[-1]['ranking_basis'] = result['ranking']['basis']
    next_offset = offset + len(rows) if offset + len(rows) < len(ordered) else None
    result["status"] = "evidence_found"
    result["returned"] = len(rows)
    result["page_source_count"] = len(rows)
    result["next_offset"] = next_offset
    result["has_more_in_window"] = next_offset is not None
    result["source_count_in_window"] = len(ordered)
    result["excerpt_count"] = sum(row["excerpt_count"] for row in rows)
    result["sources_with_excerpts"] = sum(bool(row["excerpts"]) for row in rows)
    result["results"] = rows
    return _with_field_coverage(result)
