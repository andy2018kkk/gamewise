"""按程序配置读取规则索引或分析正文；复用切分和检索，不联网或写库。"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, TypeAdapter

from app.schemas.source import SourceBatch
from app.schemas.tools import TextPassage, TextScopeInfo
from app.services.rule_chunks import split_spans
from app.services.rule_search import EmbeddingProvider, keyword_score, read_rule_context, search_rule_index


@dataclass(frozen=True)
class TextCollection:
    info: TextScopeInfo
    path: Path
    ruleset_id: str | None = None
    category: str | None = "基础规则参考"
    method: str = "keyword"
    provider: EmbeddingProvider | None = None


class TextCollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    info: TextScopeInfo
    path: str
    ruleset_id: str | None = None
    category: str | None = "基础规则参考"


def load_text_library(config_path: Path) -> "TextLibrary":
    """配置由本地程序提供，模型参数不能指定文件路径；首版配置使用关键词。"""
    configs = TypeAdapter(list[TextCollectionConfig]).validate_json(config_path.read_text(encoding="utf-8-sig"))
    collections = []
    for config in configs:
        path = Path(config.path)
        if not path.is_absolute():
            path = config_path.resolve().parent / path
        collections.append(TextCollection(config.info, path.resolve(), config.ruleset_id, config.category))
    return TextLibrary(collections)


@dataclass(frozen=True)
class _Section:
    passage: TextPassage
    body: str


def _inside(root: Path, path: str) -> Path:
    resolved = Path(path)
    resolved = (resolved if resolved.is_absolute() else root / resolved).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("资料路径超出程序配置目录")
    return resolved


def _checked(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if not expected or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("资料文件指纹不一致")
    return data


class TextLibrary:
    """文本范围白名单；历史规则及社区观点不自动映射到某个服。"""

    def __init__(self, collections: list[TextCollection]) -> None:
        ids = [c.info.scope_id for c in collections]
        if len(set(ids)) != len(ids) or any(not key.strip() for key in ids):
            raise ValueError("文本范围编号为空或重复")
        for collection in collections:
            if collection.info.record_type == "rule" and not collection.ruleset_id:
                raise ValueError("规则范围必须明确规则集")
            if collection.method not in {"keyword", "vector", "hybrid"}:
                raise ValueError("未知文本检索方式")
        self.collections = {c.info.scope_id: c for c in collections}

    def catalog(self, record_type: str) -> list[TextScopeInfo]:
        return [c.info.model_copy(deep=True) for c in self.collections.values()
                if c.info.record_type == record_type]

    def collection(self, scope_id: str, record_type: str) -> TextCollection:
        collection = self.collections.get(scope_id)
        if collection is None or collection.info.record_type != record_type:
            raise PermissionError("文本范围未获程序允许，或资料类型不匹配")
        return collection

    def _analysis_sections(self, collection: TextCollection) -> list[_Section]:
        root = collection.path.resolve()
        sources = SourceBatch.model_validate_json((root / "sources.json").read_text(encoding="utf-8-sig"))
        rows = json.loads((root / "download-report.json").read_text(encoding="utf-8"))
        by_source = {s.source_id: s for s in sources.sources}
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("分析正文清单有重复编号")
        sections = []
        for row in rows:
            if row["status"] not in {"article_text_archived", "user_text_archived", "user_text_archived_partial"}:
                continue
            source = by_source["analysis-" + row["id"].lower() + "-original"]
            if source.review_status != "pending" or source.game_id != collection.info.game_id:
                raise ValueError("资料范围或审核状态与预览契约不符")
            _checked(_inside(root, source.content_path), source.content_hash or "")
            body = _checked(_inside(root, row["text_path"]), row["text_sha256"]).decode("utf-8")
            boundaries = list(re.finditer(r"(?m)^(?:#{1,6} .+|[一二三四五六七八九十]+、[^\n]+|\d+(?:-\d+)?，[^\n]{1,80}|基础篇|身份篇)$", body))
            starts = sorted({0, *(m.start() for m in boundaries)})
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else len(body)
                text = body[start:end]
                if not text.strip():
                    continue
                heading = next((m.group().lstrip("# ") for m in boundaries if m.start() == start), source.title)
                passage = TextPassage(record_id=f"{source.source_id}:{start}", source_id=source.source_id,
                    source_url=source.source_locator, scope_id=collection.info.scope_id,
                    game_id=source.game_id, record_type="analysis", title=source.title, heading=heading,
                    text=text, start=0, end=len(text), context_hash=row["text_sha256"],
                    limitations=[collection.info.applicability, source.review_note or "社区观点，非官方规则",
                                 row["coverage"]])
                sections.append(_Section(passage, text))
        if not sections:
            raise ValueError("配置的分析资料没有可读取正文")
        return sections

    def search(self, scope_id: str, record_type: str, query: str, top_k: int) -> list[TextPassage]:
        collection = self.collection(scope_id, record_type)
        if not query.strip() or not 1 <= top_k <= 6:
            raise ValueError("文本问题或返回条数不合法")
        if record_type == "rule":
            hits = search_rule_index(collection.path, query, provider=collection.provider, top_k=top_k,
                method=collection.method, ruleset_id=collection.ruleset_id, category=collection.category)
            metadata = json.loads((collection.path / "index.json").read_text(encoding="utf-8"))
            return [TextPassage(record_id=h["chunk_id"], source_id=h["source_id"], source_url=h["source_url"],
                scope_id=scope_id, game_id=collection.info.game_id, record_type="rule", title=h["heading"],
                heading=h["heading"], text=h["text"], start=h["start"], end=h["end"],
                context_hash=metadata["chunks_sha256"], limitations=[collection.info.applicability]) for h in hits]
        hits: list[tuple[float, TextPassage]] = []
        for section in self._analysis_sections(collection):
            passage = section.passage
            for start, end in split_spans(section.body, passage.heading):
                text = section.body[start:end]
                score = keyword_score(query, passage.heading + "\n" + text)
                if score > 0:
                    hits.append((score, passage.model_copy(update={"text": text, "start": start, "end": end})))
        hits.sort(key=lambda pair: (-pair[0], pair[1].record_id, pair[1].start))
        return [passage for _, passage in hits[:top_k]]

    def read(self, passage: TextPassage, *, max_chars: int = 6000) -> TextPassage:
        if not 1 <= max_chars <= 6000:
            raise ValueError("补读长度须在1至6000之间")
        collection = self.collection(passage.scope_id, passage.record_type)
        if passage.record_type == "rule":
            metadata = json.loads((collection.path / "index.json").read_text(encoding="utf-8"))
            if metadata["chunks_sha256"] != passage.context_hash:
                raise ValueError("规则索引在查询后发生变化")
            context = read_rule_context(collection.path, passage.record_id,
                allowed_chunk_ids={passage.record_id}, max_chars=max_chars)
            return passage.model_copy(update={"text": context["text"], "start": 0,
                "end": len(context["text"]), "truncated": context["truncated"]})
        section = next((s for s in self._analysis_sections(collection)
                        if s.passage.record_id == passage.record_id), None)
        if section is None or section.passage.context_hash != passage.context_hash:
            raise ValueError("分析正文在查询后发生变化")
        return section.passage.model_copy(update={"text": section.body[:max_chars],
            "end": min(len(section.body), max_chars), "truncated": len(section.body) > max_chars})
