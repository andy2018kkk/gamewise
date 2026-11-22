"""按程序配置读取规则索引或分析正文；复用切分和检索，不联网或写库。"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.schemas.source import SourceBatch
from app.schemas.tools import TextPassage, TextScopeInfo
from app.services.rule_chunks import split_spans
from app.services.rule_search import EmbeddingProvider, keyword_score, read_rule_context, search_rule_index


REFERENCE_TOPICS = {"收益": "收益 牌差 资源", "身份": "主公 忠臣 反贼 内奸 选将",
                    "配合": "配合 团队 联动", "强度": "强度 收益 代价", "技能结算": "技能 结算 时机"}


def passage_key(passage: TextPassage) -> tuple[str, str, int, int, str]:
    """同一原文位置和版本是同一片段；不把不同出处的相似文字合并。"""
    return (passage.scope_id, passage.record_id, passage.start, passage.end, passage.context_hash)


@dataclass(frozen=True)
class TextCollection:
    info: TextScopeInfo
    path: Path
    ruleset_id: str | None = None
    category: str | None = "基础规则参考"
    method: str = "keyword"
    provider: EmbeddingProvider | None = None
    article_topics: dict[str, list[str]] | None = None


class TextCollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    info: TextScopeInfo
    path: str
    ruleset_id: str | None = None
    category: str | None = "基础规则参考"
    article_topics: dict[str, list[str]] = Field(default_factory=dict)


def load_text_library(config_path: Path) -> "TextLibrary":
    """配置由本地程序提供，模型参数不能指定文件路径；首版配置使用关键词。"""
    configs = TypeAdapter(list[TextCollectionConfig]).validate_json(config_path.read_text(encoding="utf-8-sig"))
    collections = []
    for config in configs:
        path = Path(config.path)
        if not path.is_absolute():
            path = config_path.resolve().parent / path
        collections.append(TextCollection(config.info, path.resolve(), config.ruleset_id, config.category, article_topics=config.article_topics))
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
            boundaries = list(re.finditer(r"(?m)^(?:#{1,6} [^\r\n]+|[一二三四五六七八九十]+、[^\r\n]+|\d+(?:-\d+)?，[^\r\n]{1,80}|基础篇|身份篇)\r?$", body))
            # 仅隔离已观察到的独立推广模板，不能泛删含“微信”等词的游戏说明。
            promotions = list(re.finditer(r"(?m)^[ \t]*微信搜索[^\r\n]{1,60}小程序[，,]更多精彩内容等你来看[~～!！。]?[ \t]*\r?$", body))
            excluded = [(m.start(), m.end()) for m in promotions]
            starts = sorted({0, *(m.start() for m in boundaries), *(p for span in excluded for p in span)})
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else len(body)
                text = body[start:end]
                if not text.strip() or any(a <= start < b for a, b in excluded):
                    continue
                heading = next((m.group().lstrip("# ").strip() for m in boundaries if m.start() == start), source.title)
                passage = TextPassage(record_id=f"{source.source_id}:{start}", source_id=source.source_id,
                    source_url=source.source_locator, scope_id=collection.info.scope_id,
                    game_id=source.game_id, record_type="analysis", title=source.title, heading=heading,
                    text=text, start=0, end=len(text), context_hash=row["text_sha256"],
                    topics=(collection.article_topics or {}).get(source.source_id, []),
                    limitations=[collection.info.applicability, source.review_note or "社区观点，非官方规则",
                                 row["coverage"], *(["检索视图已排除已识别的推广行；原始及整理文件未修改。"] if excluded else [])])
                sections.append(_Section(passage, text))
        if not sections:
            raise ValueError("配置的分析资料没有可读取正文")
        return sections

    def search(self, scope_id: str, record_type: str, query: str, top_k: int, *,
               topic: str | None = None, exclude: set[tuple[str, str, int, int, str]] | None = None) -> list[TextPassage]:
        collection = self.collection(scope_id, record_type)
        if not query.strip() or not 1 <= top_k <= 6:
            raise ValueError("文本问题或返回条数不合法")
        topic_words = REFERENCE_TOPICS.get(topic, topic or "")
        excluded = exclude or set()
        if record_type == "rule":
            hits = search_rule_index(collection.path, query + " " + topic_words, provider=collection.provider,
                top_k=20 if topic or excluded else top_k,
                method=collection.method, ruleset_id=collection.ruleset_id, category=collection.category)
            metadata = json.loads((collection.path / "index.json").read_text(encoding="utf-8"))
            passages = [TextPassage(record_id=h["chunk_id"], source_id=h["source_id"], source_url=h["source_url"],
                scope_id=scope_id, game_id=collection.info.game_id, record_type="rule", title=h["heading"],
                heading=h["heading"], text=h["text"], start=h["start"], end=h["end"],
                context_hash=metadata["chunks_sha256"], limitations=[collection.info.applicability]) for h in hits]
            if topic:
                passages = [p for p in passages if keyword_score(topic_words, p.heading + "\n" + p.text) > 0]
            return [p for p in passages if passage_key(p) not in excluded][:top_k]
        hits: list[tuple[float, float, TextPassage]] = []
        for section in self._analysis_sections(collection):
            passage = section.passage
            for start, end in split_spans(section.body, passage.heading):
                text = section.body[start:end]
                candidate = passage.model_copy(update={"text": text, "start": start, "end": end})
                if passage_key(candidate) in excluded:
                    continue
                topic_score = keyword_score(topic_words, passage.heading + "\n" + text) if topic else 0
                topic_match = topic in passage.topics if topic else False
                if topic and not topic_match and topic_score <= 0:
                    continue
                score = keyword_score(query, passage.heading + "\n" + text)
                if score > 0:
                    # 先限定主题相关片段，再按具体问题排序；标签不是正确性评分。
                    hits.append((float(topic_match), score + topic_score, candidate))
        hits.sort(key=lambda item: (-item[0], -item[1], item[2].record_id, item[2].start))
        return [passage for _, _, passage in hits[:top_k]]

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
