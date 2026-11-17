"""将已有SQLite查询包装为有参数校验、范围限制的只读工具。"""

from pathlib import Path
import sqlite3
from uuid import uuid4

from pydantic import ValidationError

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.tools import (CatalogArguments, ReadEvidenceArguments, SearchArguments, TextEvidence,
                               TextPassage, ToolError, ToolRecord, ToolResponse, ToolScopeResult)
from app.services.text_library import TextLibrary


class KnowledgeTools:
    """一次调用会话的查询入口；preview仅由程序配置，模型不能开启。"""

    def __init__(self, database: Path, allowed_game_ids: list[str], *, preview: bool = False,
                 max_records: int = 5, max_characters: int = 20_000,
                 text_library: TextLibrary | None = None) -> None:
        if (not allowed_game_ids or len(allowed_game_ids) > 12
                or len(set(allowed_game_ids)) != len(allowed_game_ids)
                or any(not game.strip() for game in allowed_game_ids)):
            raise ValueError("程序必须提供非空、不重复的允许游戏范围")
        if not 1 <= max_records <= 20 or max_characters < 1:
            raise ValueError("返回额度不合法")
        self.database = database
        self.allowed_game_ids = tuple(allowed_game_ids)
        self.preview = preview
        self.text_library = text_library
        self.max_records, self.max_characters = max_records, max_characters
        self._session = uuid4().hex
        self._evidence: dict[str, ToolRecord | TextEvidence] = {}

    @staticmethod
    def definitions() -> list[dict[str, object]]:
        """与模型服务无关的工具说明；服务适配器后续转换为具体API格式。"""
        return [
            {"name": "discover_catalog", "description": "record_type默认为skill查询武将目录；rule/analysis列出程序允许的文本scope_id及适用限制，不代表已审核覆盖。",
             "parameters": CatalogArguments.model_json_schema()},
            {"name": "search_records", "description": "skill用game_ids及武将等精确条件查完整技能；rule/analysis须scope_id和query，返回相关片段。三种资料不混用，模型不选择底层算法。",
             "parameters": SearchArguments.model_json_schema()},
            {"name": "read_evidence", "description": "仅凭本次返回的citation_id补读规则原段或分析章节；技能已完整返回。长文明确标记截断，不接受路径或URL。",
             "parameters": ReadEvidenceArguments.model_json_schema()},
        ]

    def resolve_citation(self, citation_id: str) -> ToolRecord | TextEvidence:
        """仅查本会话实际返回的证据；当前仍是待审预览，不能冒充正式引用。"""
        if citation_id not in self._evidence:
            raise ValueError("引用不属于本次工具会话已返回的证据")
        return self._evidence[citation_id].model_copy(deep=True)

    def invoke(self, name: str, arguments: str) -> ToolResponse:
        response = ToolResponse(preview=self.preview,
            notes=["当前只接入已加载的待审SQLite资料；未实现正式规则映射、系列展开或分页。"])
        if name == "read_evidence":
            return self._read_evidence(arguments)
        if name not in {"discover_catalog", "search_records"}:
            response.error = ToolError(code="unknown_tool", message="未登记的只读工具")
            return response
        schema = CatalogArguments if name == "discover_catalog" else SearchArguments
        try:
            parsed = schema.model_validate_json(arguments)
        except ValidationError:
            response.error = ToolError(code="invalid_arguments", message="参数必须是符合工具定义的JSON；不接受未知字段、空筛选或错误类型")
            return response
        if parsed.record_type != "skill":
            return self._text_query(name, parsed)
        game_ids = list(self.allowed_game_ids) if parsed.game_ids is None else parsed.game_ids
        if len(set(game_ids)) != len(game_ids) or any(game not in self.allowed_game_ids for game in game_ids):
            response.error = ToolError(code="scope_denied", message="查询范围重复或超出程序允许的游戏范围")
            return response
        pending_evidence: dict[str, ToolRecord] = {}
        try:
            with KnowledgeRepository(self.database, readonly=True) as repository:
                if name == "discover_catalog":
                    for item in repository.discover_catalog(**parsed.model_dump(exclude={"game_ids", "record_type"}), game_ids=game_ids):
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

    def _text_response(self, passage: TextPassage) -> ToolResponse:
        identity = f"{passage.scope_id}:{passage.record_id}:{passage.start}:{passage.end}:{passage.context_hash}"
        citation_id = f"preview:{self._session}:text:{identity}"
        evidence = TextEvidence(**passage.model_dump(exclude={"citation_id"}), citation_id=citation_id)
        return ToolResponse(preview=self.preview, results=[ToolScopeResult(game_id=passage.game_id,
            scope=self.text_library.collection(passage.scope_id, passage.record_type).info if self.text_library else None,
            status="preview_available", records=[evidence])])

    def _publish_text(self, response: ToolResponse) -> ToolResponse:
        if len(response.model_dump_json()) > self.max_characters:
            return ToolResponse(preview=self.preview, error=ToolError(code="output_limit",
                message="完整文本结果超过额度，请减少返回条数；未隐藏截断内容"))
        if response.error is not None:
            return response
        for result in response.results:
            for record in result.records:
                self._evidence[record.citation_id] = record.model_copy(deep=True)
        return response

    def _text_query(self, name: str, parsed: CatalogArguments | SearchArguments) -> ToolResponse:
        response = ToolResponse(preview=self.preview,
            notes=["文本范围为程序允许的历史规则或社区观点参考，不自动适用于任何发行分支；仅本地待审预览。"])
        if self.text_library is None:
            response.error = ToolError(code="scope_denied", message="程序未配置可查询的文本范围")
            return response
        try:
            if name == "discover_catalog":
                if any(getattr(parsed, field) is not None for field in ("game_ids", "character", "package_name", "faction")):
                    return ToolResponse(preview=self.preview, error=ToolError(code="invalid_arguments",
                        message="文本目录不接受武将筛选字段"))
                for scope in self.text_library.catalog(parsed.record_type):
                    response.results.append(ToolScopeResult(game_id=scope.game_id,
                        status="catalog_available", scope=scope))
            else:
                assert isinstance(parsed, SearchArguments) and parsed.scope_id and parsed.query
                collection = self.text_library.collection(parsed.scope_id, parsed.record_type)
                if not self.preview:
                    response.results.append(ToolScopeResult(game_id=collection.info.game_id,
                        scope=collection.info, status="pending_review"))
                else:
                    passages = self.text_library.search(parsed.scope_id, parsed.record_type, parsed.query, parsed.top_k + 1)
                    records = [self._text_response(p).results[0].records[0] for p in passages[:parsed.top_k]]
                    response.results.append(ToolScopeResult(game_id=collection.info.game_id, scope=collection.info,
                        status="preview_available" if records else "no_match_in_loaded_data", records=records,
                        has_more=len(passages) > parsed.top_k))
        except PermissionError as error:
            response.error = ToolError(code="scope_denied", message=str(error))
        except (OSError, ValueError, KeyError, ImportError) as error:
            response.error = ToolError(code="storage_error", message=f"文本资料读取或检索失败：{error}")
        return self._publish_text(response)

    def _read_evidence(self, arguments: str) -> ToolResponse:
        try:
            parsed = ReadEvidenceArguments.model_validate_json(arguments)
        except ValidationError:
            return ToolResponse(preview=self.preview, error=ToolError(code="invalid_arguments", message="补读只接受citation_id"))
        evidence = self._evidence.get(parsed.citation_id)
        if evidence is None:
            return ToolResponse(preview=self.preview, error=ToolError(code="invalid_citation", message="只能补读本会话实际返回的证据"))
        if isinstance(evidence, ToolRecord):
            return self._publish_text(ToolResponse(preview=self.preview, results=[ToolScopeResult(
                game_id=evidence.game_id, status="preview_available", records=[evidence.model_copy(deep=True)])],
                notes=["技能已完整返回，无须重复取网页。故读取同一份技能证据。"] ))
        try:
            assert self.text_library is not None
            return self._publish_text(self._text_response(self.text_library.read(evidence)))
        except (OSError, ValueError, KeyError, ImportError) as error:
            return ToolResponse(preview=self.preview, error=ToolError(code="storage_error", message=f"补读资料失败：{error}"))
