"""小规模规则索引和检索；模型适配、切分、存储分开，正式问答尚未接入。"""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol

from app.services.rule_chunks import load_rule_chunks, rule_content_type


class EmbeddingProvider(Protocol):
    identity: dict
    def count_passage(self, text: str) -> int: ...
    def encode(self, texts: list[str], *, query: bool = False): ...


def write_rule_index(processed_dir: Path, output_dir: Path, provider: EmbeddingProvider,
                     model_info: dict) -> dict:
    import numpy as np
    if provider.identity != model_info:
        raise ValueError("模型信息与实际provider不一致")
    chunks = load_rule_chunks(processed_dir, token_count=provider.count_passage)
    vectors = np.asarray(provider.encode([c.search_text for c in chunks]), dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != len(chunks) or not np.isfinite(vectors).all():
        raise ValueError("向量数量、维度或数值不合法")
    norms = np.linalg.norm(vectors, axis=1)
    if np.any(norms == 0):
        raise ValueError("不能保存零向量")
    vectors = vectors / norms[:, None]
    text = json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, sort_keys=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_path, vectors_path = output_dir / "chunks.json", output_dir / "vectors.npy"
    chunks_path.write_text(text, encoding="utf-8")
    np.save(vectors_path, vectors, allow_pickle=False)
    metadata = {"format": 1, "model": model_info, "chunks": len(chunks), "dimensions": vectors.shape[1],
                "chunks_sha256": hashlib.sha256(chunks_path.read_bytes()).hexdigest(),
                "vectors_sha256": hashlib.sha256(vectors_path.read_bytes()).hexdigest(),
                "chunking": {"max_chars": 350, "overlap_chars": 60, "max_passage_tokens": 480,
                             "overlap_alignment": "sentence_boundary"},
                "answerable": False}
    (output_dir / "index.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def _terms(text: str) -> Counter:
    clean = "".join(re.findall(r"[\w]", text.lower()))
    return Counter(clean[i:i + 2] for i in range(max(0, len(clean) - 1)))


def keyword_score(question: str, text: str) -> float:
    """共用中文双字词重合分数；不是语义判断或正确概率。"""
    terms, tokens = _terms(question), _terms(text)
    return sum(min(count, tokens[term]) for term, count in terms.items()) / max(1, sum(terms.values()))


def fuse_rankings(rankings: list[list[dict]], top_k: int, *, rank_constant: int = 60) -> list[dict]:
    """RRF按排名融合，原分数仅供诊断，不当作概率相加。"""
    if rank_constant < 1 or top_k < 1:
        raise ValueError("融合参数不合法")
    candidates = {}
    for method, hits in zip(("keyword", "vector"), rankings, strict=True):
        for rank, hit in enumerate(hits, 1):
            key = hit["chunk_id"]
            if key not in candidates:
                candidates[key] = {**hit, "score": 0.0, "retrieval": {}, "answerable": False}
            candidates[key]["score"] += 1 / (rank_constant + rank)
            candidates[key]["retrieval"][method] = {"rank": rank, "score": hit["score"]}
    ordered = sorted(candidates.values(), key=lambda h: (-h["score"], h["chunk_id"]))
    return [{**hit, "rank": rank, "score_type": "rrf"} for rank, hit in enumerate(ordered[:top_k], 1)]


def search_rule_index(index_dir: Path, question: str, *, provider: EmbeddingProvider | None,
                      top_k: int = 5, method: str = "vector", category: str | None = "基础规则参考",
                      ruleset_id: str = "sanguosha-rules-3.0", include_navigation: bool = False) -> list[dict]:
    import numpy as np
    if not question.strip() or not 1 <= top_k <= 20 or method not in {"vector", "keyword", "hybrid"}:
        raise ValueError("问题、Top K或检索方式不合法")
    if method == "hybrid":
        return fuse_rankings([search_rule_index(index_dir, question, provider=provider, top_k=20,
                    method=name, category=category, ruleset_id=ruleset_id,
                    include_navigation=include_navigation) for name in ("keyword", "vector")], top_k)
    metadata = json.loads((index_dir / "index.json").read_text(encoding="utf-8"))
    if metadata["format"] != 1:
        raise ValueError("索引格式不兼容")
    for filename, key in [("chunks.json", "chunks_sha256"), ("vectors.npy", "vectors_sha256")]:
        if hashlib.sha256((index_dir / filename).read_bytes()).hexdigest() != metadata[key]:
            raise ValueError("索引文件已变化，请重建")
    chunks = json.loads((index_dir / "chunks.json").read_text(encoding="utf-8"))
    selected = []
    for i, chunk in enumerate(chunks):
        if chunk["ruleset_id"] != ruleset_id or (category is not None and chunk["category"] != category):
            continue
        # 老索引可直接按现有来源/标题分类，向量与片段位置不变，无须重新编码。
        content_type = rule_content_type(chunk["source_url"], chunk["heading"])
        if include_navigation or content_type == "body":
            selected.append((i, {**chunk, "content_type": content_type}))
    if method == "vector":
        if provider is None:
            raise ValueError("向量检索需要已加载的本地模型")
        if provider.identity != metadata["model"]:
            raise ValueError("模型版本或编码配置与索引不匹配，请重建索引")
        vectors = np.load(index_dir / "vectors.npy", allow_pickle=False)
        if vectors.shape != (len(chunks), metadata["dimensions"]):
            raise ValueError("向量索引维度不匹配")
        query = np.asarray(provider.encode([question], query=True), dtype=np.float32)[0]
        norm = np.linalg.norm(query)
        if norm == 0 or not np.isfinite(query).all() or query.shape != (metadata["dimensions"],):
            raise ValueError("问题向量不合法或模型不兼容")
        scores = vectors @ (query / norm)
        ranked = [(float(scores[i]), c) for i, c in selected]
    else:
        ranked = []
        for _, chunk in selected:
            score = keyword_score(question, chunk["heading"] + "\n" + chunk["text"])
            if score > 0:
                ranked.append((score, chunk))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
    return [{"rank": rank, "score": score, **chunk, "answerable": False}
            for rank, (score, chunk) in enumerate(ranked[:top_k], 1)]


def read_rule_context(index_dir: Path, chunk_id: str, *, allowed_chunk_ids: set[str],
                      max_chars: int = 6000) -> dict:
    """仅允许补读本次已召回片段的完整原段；不接受路径或任意URL。"""
    if chunk_id not in allowed_chunk_ids or not 1 <= max_chars <= 6000:
        raise ValueError("只能补读本次召回的片段，且长度须在1至6000之间")
    metadata = json.loads((index_dir / "index.json").read_text(encoding="utf-8"))
    raw = (index_dir / "chunks.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata["chunks_sha256"]:
        raise ValueError("索引文件已变化，请重建")
    chunks = json.loads(raw)
    selected = next((c for c in chunks if c["chunk_id"] == chunk_id), None)
    if selected is None:
        raise ValueError("片段不存在于当前索引")
    siblings = sorted((c for c in chunks if c["record_id"] == selected["record_id"]), key=lambda c: c["start"])
    text = ""
    for chunk in siblings:
        start, end = chunk["start"], chunk["end"]
        if start > len(text) or end - start != len(chunk["text"]):
            raise ValueError("原段片段不连续或字符位置不一致")
        shared = min(len(text), end) - start
        if text[start:start + shared] != chunk["text"][:shared]:
            raise ValueError("重叠原文不一致")
        text += chunk["text"][shared:]
    return {"record_id": selected["record_id"], "chunk_id": chunk_id, "heading": selected["heading"],
            "source_url": selected["source_url"], "ruleset_id": selected["ruleset_id"],
            "text": text[:max_chars], "truncated": len(text) > max_chars, "answerable": False}
