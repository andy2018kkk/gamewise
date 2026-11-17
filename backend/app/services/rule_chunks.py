"""规则文件到检索片段：保留原文字符范围，不下载模型或写数据库。"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from app.schemas.rule import RuleRecord
from app.services.data_processor import ProcessingResult


def rule_content_type(source_url: str, heading: str) -> Literal["body", "navigation"]:
    """只识别索引页中明确命名的导引段；同页提要和正文仍保留。"""
    section = heading.rsplit("/", 1)[-1].strip()
    if urlsplit(source_url).path.endswith("/index.html") and section.endswith(("导引", "目录", "索引")):
        return "navigation"
    return "body"


@dataclass(frozen=True)
class RuleChunk:
    chunk_id: str
    record_id: str
    source_id: str
    source_url: str
    ruleset_id: str
    category: str
    heading: str
    text: str
    start: int
    end: int

    @property
    def search_text(self) -> str:
        return f"{self.heading}\n{self.text}"

    def to_dict(self) -> dict:
        return {**asdict(self), "content_type": rule_content_type(self.source_url, self.heading)}


def split_spans(text: str, heading: str, *, max_chars: int = 350, overlap: int = 60,
                token_count: Callable[[str], int] | None = None, max_tokens: int = 480) -> list[tuple[int, int]]:
    if max_chars < 2 or not 0 <= overlap < max_chars or not text.strip() or max_tokens < 1:
        raise ValueError("切分长度、重叠或文本不合法")
    spans, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # 使用实际模型的 tokenizer 检查标题、前缀和正文的总长度，不静默截断。
        if token_count is not None:
            while end > start and token_count(f"{heading}\n{text[start:end]}") > max_tokens:
                end -= max(1, (end - start) // 10)
            if end <= start:
                raise ValueError("标题过长，无法在模型长度限制内保留正文")
        if end < len(text):
            boundaries = [m.end() for m in re.finditer(r"\n|[。！？；]", text[start:end])]
            boundaries = [start + b for b in boundaries if b >= (end - start) // 2]
            if boundaries:
                end = boundaries[-1]
        spans.append((start, end))
        if end == len(text):
            break
        next_start = max(start + 1, end - min(overlap, (end - start) // 3))
        # 优先从完整句子或段落开始重叠；没有边界的超长句仍保留精确字符范围。
        boundaries = list(re.finditer(r"\n|[。！？；]", text[next_start:end]))
        start = next_start + boundaries[0].end() if boundaries else next_start
    return spans


def load_rule_chunks(directory: Path, *, token_count: Callable[[str], int] | None = None) -> list[RuleChunk]:
    chunks = []
    report = directory / "report.json"
    if not report.is_file():
        raise ValueError("缺少规则处理清单")
    inventory = json.loads(report.read_text(encoding="utf-8"))["inventory"]
    for page in inventory:
        path = (directory / page["json"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("规则文件越过输入目录")
        batch = ProcessingResult[RuleRecord].model_validate_json(path.read_text(encoding="utf-8"))
        for record in batch.records:
            data = record.data
            for start, end in split_spans(data.text, data.heading, token_count=token_count):
                identity = f"{record.record_id}:{start}:{end}:{data.text[start:end]}"
                chunks.append(RuleChunk(
                    chunk_id=hashlib.sha256(identity.encode()).hexdigest(), record_id=record.record_id,
                    source_id=batch.source.source_id, source_url=batch.source.source_locator,
                    ruleset_id=data.ruleset_id, category=batch.source.topic,
                    heading=data.heading, text=data.text[start:end], start=start, end=end,
                ))
    if not chunks or len({c.chunk_id for c in chunks}) != len(chunks):
        raise ValueError("规则片段为空或编号重复")
    return chunks
