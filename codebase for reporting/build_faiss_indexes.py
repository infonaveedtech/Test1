import os, sys, json, time, math, ujson, faiss, argparse
from pathlib import Path
from typing import List, Dict, Any
from tqdm import tqdm
from sentence_transformers import SentenceTransformer

# --------- Settings (edit if needed) ----------
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM   = 384
BATCH       = 64
VERSION     = "2025-11-10"  # bump when your docs change
ROOT        = Path("data")
DOCS_DIR    = ROOT / "docs"
FAISS_DIR   = ROOT / "faiss"
REGISTRY    = ROOT / "registry.json"

FEWSHOTS_JSONL = DOCS_DIR / "fewshots.docs.jsonl"
GLOSSARY_JSONL = DOCS_DIR / "glossary.docs.jsonl"

# fewshots: exact search (tiny corpus)
FEWSHOTS_INDEX_TYPE = "FlatIP"    # IndexFlatIP

# glossary: scalable HNSW
GLOSSARY_INDEX_TYPE = "HNSW"      # IndexHNSWFlat (M=64, efConstruction=200)
HNSW_M              = 64
HNSW_EF_CONSTRUCT   = 200

# ----------------------------------------------

def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    docs = []
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(ujson.loads(line))
            except Exception as e:
                raise ValueError(f"Invalid JSON on line {ln} in {path}: {e}")
    return docs

def concat_text(doc: Dict[str, Any], fields: List[str]) -> str:
    parts = []
    for k in fields:
        v = doc.get(k, "")
        if v is None:
            v = ""
        parts.append(str(v).strip())
    return "\n".join([p for p in parts if p])

def embed_all(model: SentenceTransformer, texts: List[str], batch_size: int = 64):
    vecs = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Embedding"):
        batch = texts[i:i+batch_size]
        emb = model.encode(batch, normalize_embeddings=True)  # cosine via IP
        vecs.append(emb)
    import numpy as np
    return np.vstack(vecs).astype("float32")

def build_index(vectors, index_type: str):
    dim = vectors.shape[1]
    if index_type == "FlatIP":
        index = faiss.IndexFlatIP(dim)
    elif index_type == "HNSW":
        index = faiss.IndexHNSWFlat(dim, HNSW_M)
        index.hnsw.efConstruction = HNSW_EF_CONSTRUCT
    else:
        raise ValueError("Unknown index_type")
    index.add(vectors)
    return index

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def write_jsonl(path: Path, rows: List[Dict[str, Any]]):
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(ujson.dumps(r, ensure_ascii=False) + "\n")

def build_collection(name: str,
                     docs: List[Dict[str, Any]],
                     text_fields: List[str],
                     meta_fields: List[str],
                     index_type: str):
    out_dir = FAISS_DIR / f"{name}_v{VERSION}"
    ensure_dir(out_dir)

    # 1) compose text to embed
    texts = [concat_text(d, text_fields) for d in docs]
    ids   = [d["id"] for d in docs]
    metas = [{k: d.get(k) for k in meta_fields} | {"id": d["id"]} for d in docs]

    # 2) embed
    print(f"[{name}] loading embedding model:", EMBED_MODEL)
    model = SentenceTransformer(EMBED_MODEL)
    vecs  = embed_all(model, texts, BATCH)

    # 3) build FAISS
    print(f"[{name}] building FAISS index type:", index_type)
    index = build_index(vecs, "FlatIP" if index_type=="FlatIP" else "HNSW")

    # 4) save
    faiss.write_index(index, str(out_dir / "index.faiss"))
    write_jsonl(out_dir / "ids.jsonl", [{"pos": i, "id": ids[i]} for i in range(len(ids))])
    write_jsonl(out_dir / "metadatas.jsonl", metas)

    # 5) write config
    config = {
        "collection_name": name,
        "version": VERSION,
        "embedding_model": EMBED_MODEL,
        "dimension": EMBED_DIM,
        "normalize_embeddings": True,
        "similarity": "cosine",
        "faiss_index_type": "IndexFlatIP" if index_type=="FlatIP" else "IndexHNSWFlat",
        "faiss_params": {} if index_type=="FlatIP" else {"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCT},
        "build_stats": {"doc_count": len(ids), "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    }
    (out_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(f"[{name}] saved -> {out_dir}  (docs={len(ids)})")
    return out_dir

def update_registry(path: Path, few_path: Path, glo_path: Path):
    reg = {}
    if path.exists():
        try:
            reg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            reg = {}
    # Always store POSIX-style paths; Windows accepts these too
    reg["fewshots"] = {"active_path": few_path.as_posix(), "version": VERSION}
    reg["glossary"] = {"active_path": glo_path.as_posix(), "version": VERSION}
    path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    print("[registry] updated:", path)


def main():
    # 0) load docs
    few_docs = load_jsonl(FEWSHOTS_JSONL)
    glo_docs = load_jsonl(GLOSSARY_JSONL)

    # 1) build fewshots (embed title+question+sql)
    few_path = build_collection(
        name="fewshots",
        docs=few_docs,
        text_fields=["title","question","sql_text"],
        meta_fields=["tables","metrics","grain","time_scope","date_columns","labels_used","notes"],
        index_type=FEWSHOTS_INDEX_TYPE
    )

    # 2) build glossary (embed title+summary+content)
    glo_path = build_collection(
        name="glossary",
        docs=glo_docs,
        text_fields=["title","summary","content"],
        meta_fields=["doc_type","chapter","section","tables","canonical_joins","labels_vs_ids","metrics_keywords","importance","version"],
        index_type=GLOSSARY_INDEX_TYPE
    )

    # 3) update registry
    update_registry(REGISTRY, few_path, glo_path)

if __name__ == "__main__":
    main()
