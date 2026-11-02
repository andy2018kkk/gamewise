"""将已有SQLite查询包装为有参数校验、范围限制的只读工具。"""

from pathlib import Path
import sqlite3
from uuid import uuid4

from pydantic import ValidationError

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.tools import (CatalogArguments, SearchArguments, ToolError, ToolRecord,
                               ToolResponse, ToolScopeResult)


class KnowledgeTools:
    """一次调用会话的查询入口；preview仅由程序配置，模型不能开启。"""

    def __init__(self, database: Path, allowed_game_ids: list[str], *, preview: bool = False,
                 max_records: int = 5, max_characters: int = 20_000) -> None:
        if (not allowed_game_ids or len(allowed_game_ids) > 12
                or len(set(allowed_game_ids)) != len(allowed_game_ids)
                or any(not game.strip() for game in allowed_game_ids)):
            raise ValueError("程序必须提供非空、不重复的允许游戏范围")
        if not 1 <= max_records <= 20 or max_characters < 1:
            raise ValueError("返回额度不合法")
        self.database = database
        self.allowed_game_ids = tuple(allowed_game_ids)
        self.preview = preview
        self.max_records, self.max_characters = max_records, max_characters
        self._session = uuid4().hex
        self._evidence: dict[str, ToolRecord] = {}

    @staticmethod
    def definitions() -> list[dict[str, object]]:
        """与模型服务无关的工具说明；服务适配器后续转换为具体API格式。"""
        return [
            {"name": "discover_catalog", "description": "查询已加载资料的武将、模式、包和势力；不代表完整游戏目录或已审核覆盖。",
             "parameters": CatalogArguments.model_json_schema()},
            {"name": "search_records", "description": "按武将、技能名、包或势力的交集查询完整技能；只在允许的游戏范围内查询，不修改资料。",
             "parameters": SearchArguments.model_json_schema()},
        ]

    def resolve_citation(self, citation_id: str) -> ToolRecord:
        """仅查本会话实际返回的证据；当前仍是待审预览，不能冒充正式引用。"""
        if citation_id not in self._evidence:
            raise ValueError("引用不属于本次工具会话已返回的证据")
        return self._evidence[citation_id].model_copy(deep=True)

    def invoke(self, name: str, arguments: str) -> ToolResponse:
        response = ToolResponse(preview=self.preview,
            notes=["当前只接入已加载的待审SQLite资料；未实现正式规则映射、系列展开或分页。"])
        if name not in {"discover_catalog", "search_records"}:
            response.error = ToolError(code="unknown_tool", message="未登记的只读工具")
            return response
        schema = CatalogArguments if name == "discover_catalog" else SearchArguments
        try:
            parsed = schema.model_validate_json(arguments)
        except ValidationError:
            response.error = ToolError(code="invalid_arguments", message="参数必须是符合工具定义的JSON；不接受未知字段、空筛选或错误类型")
            return response
        game_ids = list(self.allowed_game_ids) if parsed.game_ids is None else parsed.game_ids
        if len(set(game_ids)) != len(game_ids) or any(game not in self.allowed_game_ids for game in game_ids):
            response.error = ToolError(code="scope_denied", message="查询范围重复或超出程序允许的游戏范围")
            return response
        pending_evidence: dict[str, ToolRecord] = {}
        try:
            with KnowledgeRepository(self.database, readonly=True) as repository:
                if name == "discover_catalog":
                    for item in repository.discover_catalog(**parsed.model_dump(exclude={"game_ids"}), game_ids=game_ids):
                        more = any(len(getattr(item, key)) > 20 for key in ("characters", "packages", "factions", "modes"))
                        item = item.model_copy(update={key: getattr(item, key)[:20]
                            for key in ("characters", "packages", "factions", "modes")})
                        response.results.append(ToolScopeResult(game_id=item.game_id, status="catalog_available",
                                                               catalog=item, has_more=more))
                else:
                    assert isinstance(parsed, SearchArguments)
                    for item in repository.search_records(game_ids=game_ids, **parsed.filters().model_dump()):
                        if not item.records:
                            status = "not_loaded" if item.status == "not_collected" else "no_match_in_loaded_data"
                            response.results.append(ToolScopeResult(game_id=item.game_id, status=status))
                            continue
                        if not self.preview:
                            response.results.append(ToolScopeResult(game_id=item.game_id, status="pending_review"))
                            continue
                        records = []
                        for record in item.records[:self.max_records]:
                            citation_id = f"preview:{self._session}:{record.record_id}"
                            evidence = ToolRecord(**record.model_dump(), citation_id=citation_id)
                            records.append(evidence)
                            pending_evidence[citation_id] = evidence.model_copy(deep=True)
                        response.results.append(ToolScopeResult(game_id=item.game_id, status="preview_available",
                            records=records, has_more=len(item.records) > len(records)))
            if len(response.model_dump_json()) > self.max_characters:
                return ToolResponse(preview=self.preview, error=ToolError(code="output_limit",
                    message="完整结果超过返回额度，请缩小范围或筛选条件；未截断技能正文"))
        except (sqlite3.Error, OSError):
            return ToolResponse(preview=self.preview, error=ToolError(code="storage_error", message="本地资料存储读取失败"))
        except ValueError as error:
            return ToolResponse(preview=self.preview, error=ToolError(code="storage_error", message=str(error)))
        self._evidence.update(pending_evidence)
        return response
