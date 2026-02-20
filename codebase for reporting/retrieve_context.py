import json, re, faiss, numpy as np
from pathlib import Path
from typing import List, Dict, Any, Tuple
from sentence_transformers import SentenceTransformer
from rank_bm25 import BM25Okapi

# ------------------ Paths / global state ------------------
# REGISTRY = json.loads(Path("data/registry.json").read_text())
REGISTRY = json.loads(Path("data/registry.json").read_text())

def _normalize_path(p: str) -> Path:
    """
    Accept paths with either backslashes or forward slashes and
    normalize to a Path that works on both Windows and Linux.
    """
    return Path(p.replace("\\", "/"))

EMB = SentenceTransformer("BAAI/bge-small-en-v1.5")
TOPK_FAISS = 50          # initial candidate pool from FAISS per collection
TOPK_RETURN_FEWS = 5     # final few-shots to return
TOPK_RETURN_GLOSS = 6    # final glossary pages to return
RANDOM_SEED = 13

# ------------------ Utilities ------------------
def simple_tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_\.]+", (text or "").lower())

def load_collection(name: str):
    """Load FAISS + ids + metas and also raw source docs text for BM25."""
    raw_path = REGISTRY[name]["active_path"]
    path = _normalize_path(raw_path)
    # path = Path(REGISTRY[name]["active_path"])
    index = faiss.read_index(str(path / "index.faiss"))
    try:
        index.hnsw.efSearch = 96    # quality knob for HNSW (no-op for FlatIP)
    except AttributeError:
        pass
    ids   = [json.loads(l)["id"] for l in (path / "ids.jsonl").read_text().splitlines()]
    metas = [json.loads(l)       for l in (path / "metadatas.jsonl").read_text().splitlines()]
    # Source jsonl (to reconstruct text for BM25/scoring)
    src_file = "data/docs/fewshots.docs.jsonl" if name=="fewshots" else "data/docs/glossary.docs.jsonl"
    raw = {json.loads(l)["id"]: json.loads(l) for l in Path(src_file).read_text(encoding="utf-8").splitlines()}
    # Build text field generically
    texts = []
    for mid in ids:
        d = raw.get(mid, {})
        parts = []
        for k in ("title","summary","content","question","sql_text"):
            v = d.get(k)
            if v: parts.append(v)
        texts.append("\n".join(parts))
    return index, ids, metas, texts, raw

def embed_query(q: str):
    return EMB.encode([q], normalize_embeddings=True).astype("float32")

def faiss_candidates(name: str, query: str, k: int = TOPK_FAISS) -> List[Dict[str,Any]]:
    index, ids, metas, texts, raw = load_collection(name)
    qv = embed_query(query)
    D, I = index.search(qv, k)
    cands = []
    for rank, pos in enumerate(I[0]):
        if pos < 0: continue
        mid = ids[pos]
        cands.append({
            "rank": rank,
            "pos": int(pos),
            "id": mid,
            "meta": metas[pos],
            "text": texts[pos],
            "raw": raw.get(mid, {})
        })
    return cands

def bm25_scores(query: str, docs_text: List[str]) -> np.ndarray:
    tokenized = [simple_tokens(t) for t in docs_text]
    bm25 = BM25Okapi(tokenized)
    qtok = simple_tokens(query)
    return np.array(bm25.get_scores(qtok), dtype="float32")

def jaccard(a, b) -> float:
    A, B = set(a), set(b)
    if not A and not B: return 0.0
    return len(A & B) / max(1.0, float(len(A | B)))

def normalize(arr: np.ndarray) -> np.ndarray:
    if len(arr)==0: return arr
    lo, hi = float(np.min(arr)), float(np.max(arr))
    if hi - lo < 1e-8: return np.zeros_like(arr, dtype="float32")
    return (arr - lo) / (hi - lo)

def rerank_generic(query: str, candidates: List[Dict[str,Any]], topk: int) -> List[Dict[str,Any]]:
    # 1) semantic = inverse of faiss rank
    sem = np.array([-(c["rank"]) for c in candidates], dtype="float32")
    sem = normalize(sem)
    # 2) BM25 lexical
    bm25 = bm25_scores(query, [c["text"] for c in candidates])
    bm25 = normalize(bm25)
    # 3) metadata overlap (generic)
    meta = []
    qtok = set(simple_tokens(query))
    for c in candidates:
        m = c["meta"] or {}
        mkw = m.get("metrics_keywords") or m.get("metrics") or []
        tb  = m.get("tables") or []
        tb_tok = []
        for t in tb: tb_tok += simple_tokens(t)
        j_metrics = jaccard(qtok, set(map(str.lower, mkw)))
        j_tables  = jaccard(qtok, set(tb_tok))
        meta.append(0.5*j_metrics + 0.3*j_tables)
    meta = normalize(np.array(meta, dtype="float32"))
    # Blend (generic weights)
    alpha, beta, gamma = 0.5, 0.35, 0.15
    blend = alpha*sem + beta*bm25 + gamma*meta
    order = np.argsort(-blend)
    ranked = [candidates[i] | {"score": float(blend[i])} for i in order]
    # MMR diversification (light)
    return mmr_diversify(query, ranked, take=topk)

def mmr_diversify(query: str, ranked: List[Dict[str,Any]], take: int, lambda_: float = 0.7):
    texts = [r["text"] for r in ranked]
    vecs  = EMB.encode(texts, normalize_embeddings=True)
    qv    = EMB.encode([query], normalize_embeddings=True)[0]
    picked, rest = [], list(range(len(ranked)))
    while rest and len(picked) < take:
        best_i, best_s = None, -1e9
        for i in rest:
            sim_q = float(np.dot(qv, vecs[i]))
            sim_p = 0.0
            if picked:
                sim_p = max(float(np.dot(vecs[i], vecs[j])) for j in picked)
            score = lambda_ * sim_q - (1 - lambda_) * sim_p
            if score > best_s:
                best_s, best_i = score, i
        picked.append(best_i)
        rest.remove(best_i)
    return [ranked[i] for i in picked]

# ------------------ Chapter selection & kit ------------------
# --- add this helper near the top ---
def embed_texts(texts):
    return EMB.encode(texts, normalize_embeddings=True).astype("float32")

def chapter_centroids(glossary_cands):
    # Build {chapter: [texts]} from candidate pool
    by_ch = {}
    for d in glossary_cands:
        raw = d["raw"]
        ch  = raw.get("chapter")
        if not ch: 
            continue
        # compose a compact text for centroid
        parts = [raw.get("title",""), raw.get("summary",""), raw.get("content","")]
        txt   = "\n".join([p for p in parts if p])
        if not txt.strip():
            continue
        by_ch.setdefault(ch, []).append(txt)
    # average pooled embeddings per chapter
    centroids = {}
    for ch, texts in by_ch.items():
        vecs = embed_texts(texts)
        centroids[ch] = np.mean(vecs, axis=0)
        # normalize to unit for cosine
        centroids[ch] = centroids[ch] / max(1e-8, np.linalg.norm(centroids[ch]))
    return centroids

def select_chapters(query: str, glossary_cands, topn: int = 2) -> list[str]:
    qv = embed_texts([query])[0]
    cents = chapter_centroids(glossary_cands)
    if not cents:
        return []
    # compute cosine sims
    sims = [(ch, float(np.dot(qv, vec))) for ch, vec in cents.items()]
    sims.sort(key=lambda x: x[1], reverse=True)
    # choose primary + maybe secondary (margin-based)
    primary, s1 = sims[0]
    chosen = [primary]
    if len(sims) > 1:
        second, s2 = sims[1]
        if s2 >= s1 - 0.05 and s2 >= 0.35:   # margin + floor
            chosen.append(second)
    return chosen[:topn]


def build_chapter_table_set(glossary_docs: List[Dict[str,Any]], chapters: List[str]) -> set:
    tables = set()
    for d in glossary_docs:
        if d["raw"].get("chapter") in chapters:
            for t in (d["raw"].get("tables") or []):
                tables.add(t)
    return tables

def filter_glossary_by_chapters(glossary_docs: List[Dict[str,Any]], chapters: List[str]) -> List[Dict[str,Any]]:
    return [d for d in glossary_docs if (d["raw"].get("chapter") in chapters or d["raw"].get("chapter") == "Global")]

def ensure_coverage(glossary_ranked: List[Dict[str,Any]], need_recipes:int = 3) -> List[Dict[str,Any]]:
    """Guarantee a coherent kit:
       - >=1 overview
       - >=1 time semantics (date_semantics present or section mentions 'Time semantics')
       - >=1 canonical_joins (canonical_joins non-empty)
       - >=1 labels/participants (labels_vs_ids non-empty)
       - fill with top recipes/concepts up to TOPK_RETURN_GLOSS
    """
    out = []
    has_overview = False
    has_time     = False
    has_joins    = False
    has_labels   = False
    # first pass: pick essentials if encountered early
    for g in glossary_ranked:
        if len(out) >= TOPK_RETURN_GLOSS:
            break
        raw = g["raw"]
        dt  = raw.get("doc_type")
        if not has_overview and dt == "chapter_overview":
            out.append(g); has_overview = True; continue
        # date semantics detection: explicit date_semantics obj OR section/title mentions time semantics
        ds = raw.get("date_semantics")
        if not has_time and (ds or str(raw.get("section","")).lower().find("time")>=0 or str(raw.get("title","")).lower().find("time")>=0):
            out.append(g); has_time = True; continue
        # canonical joins
        if not has_joins and (raw.get("canonical_joins") or []):
            out.append(g); has_joins = True; continue
        # labels/participants
        if not has_labels and (raw.get("labels_vs_ids") or []):
            out.append(g); has_labels = True; continue
    # second pass: add top recipes/concepts to get depth
    for g in glossary_ranked:
        if len(out) >= TOPK_RETURN_GLOSS:
            break
        raw = g["raw"]
        if raw in [x["raw"] for x in out]:
            continue
        if raw.get("doc_type") in ("recipe","concept","section_note","gotcha") and raw.get("chapter") != "Global":
            out.append(g)
    # if we still lack essentials, append any remaining matching types
    if not has_overview:
        any_ov = next((g for g in glossary_ranked if g["raw"].get("doc_type")=="chapter_overview"), None)
        if any_ov and any_ov not in out and len(out) < TOPK_RETURN_GLOSS:
            out.append(any_ov)
    if not has_time:
        any_tm = next((g for g in glossary_ranked if g["raw"].get("date_semantics")), None)
        if any_tm and any_tm not in out and len(out) < TOPK_RETURN_GLOSS:
            out.append(any_tm)
    if not has_joins:
        any_jn = next((g for g in glossary_ranked if g["raw"].get("canonical_joins")), None)
        if any_jn and any_jn not in out and len(out) < TOPK_RETURN_GLOSS:
            out.append(any_jn)
    if not has_labels:
        any_lb = next((g for g in glossary_ranked if g["raw"].get("labels_vs_ids")), None)
        if any_lb and any_lb not in out and len(out) < TOPK_RETURN_GLOSS:
            out.append(any_lb)
    # include one small Global guardrail if room
    if len(out) < TOPK_RETURN_GLOSS:
        any_glob = next((g for g in glossary_ranked if g["raw"].get("chapter")=="Global"), None)
        if any_glob and any_glob not in out:
            out.append(any_glob)
    return out[:TOPK_RETURN_GLOSS]

def rerank_within_chapters(query: str, chapter_docs: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    # Use the same generic reranker; it’s already chapter-filtered
    return rerank_generic(query, chapter_docs, topk=min(len(chapter_docs), TOPK_RETURN_GLOSS*3))

# ------------------ Few-shots alignment ------------------
def align_fewshots(query: str, chapter_tables: set) -> List[Dict[str,Any]]:
    # 1) candidates from FAISS
    few = faiss_candidates("fewshots", query, k=TOPK_FAISS)
    if not few: return []
    # 2) generic rerank first
    few_r = rerank_generic(query, few, topk=min(len(few), 20))
    # 3) soft boost for overlap with chapter tables (no hardcoded table names)
    scores = []
    for i, c in enumerate(few_r):
        tb = set(c["raw"].get("tables") or [])
        overlap = len(tb & chapter_tables)
        # blend base rank with overlap
        scores.append((i, c, overlap))
    # sort by overlap then by current rank order
    scores.sort(key=lambda x: (-x[2], x[0]))
    aligned = [c for _, c, _ in scores][:TOPK_RETURN_FEWS]
    return aligned

# ------------------ Main retrieval API ------------------
def retrieve_context(query: str) -> Dict[str,Any]:
    # Step 1: get a broad candidate pool from glossary
    gl_cands = faiss_candidates("glossary", query, k=TOPK_FAISS)
    # Step 2: select top chapters by chapter_overview similarity
    chapters = select_chapters(query, gl_cands, topn=2)
    if not chapters:
        chapters = ["Global"]  # degenerate fallback
    # Step 3: filter glossary to those chapters (+ Global), rerank, ensure coverage
    gl_ch_docs = filter_glossary_by_chapters(gl_cands, chapters)
    gl_ranked  = rerank_within_chapters(query, gl_ch_docs)
    gl_final   = ensure_coverage(gl_ranked)
    # Step 4: derive chapter table set and align few-shots
    chapter_tables = build_chapter_table_set(gl_cands, chapters)
    fs_final = align_fewshots(query, chapter_tables)
    # Step 5: assemble a compact, human-readable result
    return {
        "query": query,
        "chapters_selected": chapters,
        "fewshots": [
            {
                "id": f["id"],
                "title": f["raw"].get("title"),
                "question": f["raw"].get("question"),
                "tables": f["raw"].get("tables"),
            } for f in fs_final
        ],
        "glossary": [
            {
                "id": g["id"],
                "doc_type": g["raw"].get("doc_type"),
                "chapter": g["raw"].get("chapter"),
                "section": g["raw"].get("section"),
                "title": g["raw"].get("title"),
            } for g in gl_final
        ]
    }

# ------------------ CLI test ------------------
if __name__ == "__main__":
    tests = [
        "95th percentile spread by exchange in the last 30 days",
        "users who are buying more and selling less in a symbol",
        "daily levy totals by market & side for the last 30 days",
        "repo cancellations vs executed ratio by symbol this quarter",
    ]
    for q in tests:
        ctx = retrieve_context(q)
        print("\n====================")
        print("Query:", ctx["query"])
        print("Chapters:", ctx["chapters_selected"])
        print("Few-shots:", [x["id"] for x in ctx["fewshots"]])
        print("Glossary:", [x["id"] for x in ctx["glossary"]])
