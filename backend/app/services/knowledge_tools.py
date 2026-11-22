"""将已有SQLite查询包装为有参数校验、范围限制的只读工具。"""

from pathlib import Path
import json
import hashlib
import sqlite3
from uuid import uuid4

from pydantic import ValidationError

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import QueryFilters
from app.schemas.tools import (CatalogArguments, CharacterSkillsArguments, SkillTarget, ReferenceSearchArguments, ReadEvidenceArguments, SearchArguments, TextEvidence,
                               TextPassage, ToolError, ToolRecord, ToolResponse, ToolScopeResult)
from app.services.text_library import REFERENCE_TOPICS, TextLibrary, passage_key
from app.services.character_families import CharacterFamilies


class KnowledgeTools:
    """一次调用会话的查询入口；preview仅由程序配置，模型不能开启。"""

    def __init__(self, database: Path, allowed_game_ids: list[str], *, preview: bool = False,
                 max_records: int = 5, max_characters: int | None = 20_000,
                 text_library: TextLibrary | None = None,
                 character_families: CharacterFamilies | None = None) -> None:
        if (not allowed_game_ids or len(allowed_game_ids) > 12
                or len(set(allowed_game_ids)) != len(allowed_game_ids)
                or any(not game.strip() for game in allowed_game_ids)):
            raise ValueError("程序必须提供非空、不重复的允许游戏范围")
        if not 1 <= max_records <= 20 or (max_characters is not None and max_characters < 1):
            raise ValueError("返回额度不合法")
        self.database = database
        self.allowed_game_ids = tuple(allowed_game_ids)
        self.preview = preview
        self.text_library = text_library
        self.character_families = character_families
        self.max_records, self.max_characters = max_records, max_characters
        self._session = uuid4().hex
        self._evidence: dict[str, ToolRecord | TextEvidence] = {}

    @staticmethod
    def definitions() -> list[dict[str, object]]:
        """与模型服务无关的工具说明；服务适配器后续转换为具体API格式。"""
        catalog = CatalogArguments.model_json_schema()
        catalog["properties"].pop("record_type")
        catalog["properties"]["game_ids"]["description"] = "可选发行分支；省略时分别查程序允许的服。"
        catalog["properties"]["character"]["description"] = "人物名或具体武将名。"
        catalog["properties"]["include_related"]["description"] = "人物推荐未指定系列时设true，发现该人物相关系列。"
        return [
            {"name": "discover_catalog", "description": "查武将有哪些系列和模式；人物名加include_related=true可发现相关系列，不读技能或文章。",
             "parameters": catalog},
            {"name": "get_character_skills", "description": "查人物或具体武将的完整技能。人物名自动返回有来源的各系列及技能，无须先查目录；具体系列精确查。服和模式可选，不搜索文章。",
             "parameters": CharacterSkillsArguments.model_json_schema()},
            {"name": "search_reference", "description": "搜索规则或策略文章。策略库是通用分析框架，不是武将排行榜：从已读技能提炼机制、收益、代价或身份目标，用这些概念检索，不用人物名找现成强弱结论。规则按机制结算检索。程序选择资料集合。",
             "parameters": ReferenceSearchArguments.model_json_schema()},
            {"name": "read_evidence", "description": "需要上下文时，用本次资料编号补读原段；技能已完整返回，不必补读。",
             "parameters": ReadEvidenceArguments.model_json_schema()},
        ]

    def _get_character_skills(self, arguments: str) -> ToolResponse:
        try:
            parsed = CharacterSkillsArguments.model_validate_json(arguments)
        except ValidationError:
            return ToolResponse(preview=self.preview, error=ToolError(code="invalid_arguments",
                message="请填写characters武将名称列表，可选game_ids和mode；文章请用search_reference。"))
        game_ids = list(self.allowed_game_ids) if parsed.game_ids is None else parsed.game_ids
        if len(set(game_ids)) != len(game_ids) or any(game not in self.allowed_game_ids for game in game_ids):
            return ToolResponse(preview=self.preview, error=ToolError(code="scope_denied", message="查询范围重复或超出程序允许的游戏范围"))
        response = ToolResponse(preview=self.preview)
        previous = dict(self._evidence)
        pending: dict[str, ToolRecord] = {}
        seen: set[tuple[str, str]] = set()
        try:
            with KnowledgeRepository(self.database, readonly=True) as repository:
                for game_id in game_ids:
                    for character in dict.fromkeys(parsed.characters):
                        names = [character]
                        if self.character_families is not None:
                            _, family = self.character_families.discover(repository, game_id, character)
                            if family.status == "registered":
                                names = list(dict.fromkeys([character, *[c.character for c in family.candidates]]))
                            elif family.status == "conflict":
                                response.notes.append(f"{game_id}/{character}的人物关联冲突，仅查询同名条目，不能声称系列完整。")
                        else:
                            response.notes.append(f"{game_id}/{character}未配置人物关联目录，仅查询同名条目，不能声称系列完整。")
                        for name in names:
                            if (game_id, name) in seen:
                                continue
                            seen.add((game_id, name))
                            item, = repository.search_records(game_ids=[game_id], character=name, mode=parsed.mode)
                            if not item.records:
                                response.results.append(ToolScopeResult(game_id=game_id,
                                    status="not_loaded" if item.status == "not_collected" else "no_match_in_loaded_data"))
                                response.notes.append(f"{game_id}/{name}：所选模式未收录技能。")
                                continue
                            if not self.preview:
                                response.results.append(ToolScopeResult(game_id=game_id, status="pending_review"))
                                continue
                            groups: dict[str, list[ToolRecord]] = {}
                            for record in item.records:
                                citation_id = self._citation_id("skill:" + record.record_id)
                                evidence = ToolRecord(**record.model_dump(), citation_id=citation_id)
                                groups.setdefault(record.mode, []).append(evidence)
                                pending[citation_id] = evidence.model_copy(deep=True)
                            for mode, records in groups.items():
                                response.results.append(ToolScopeResult(game_id=game_id,
                                    target=SkillTarget(game_id=game_id, character=name, mode=mode),
                                    status="preview_available", records=records))
        except (sqlite3.Error, OSError, ValueError):
            return ToolResponse(preview=self.preview, error=ToolError(code="storage_error", message="本地人物关联或技能资料读取失败"))
        published = self._publish_text(response)
        if published.error:
            self._evidence = previous
        else:
            self._evidence.update(pending)
        return published

    def _search_reference(self, arguments: str) -> ToolResponse:
        try:
            parsed = ReferenceSearchArguments.model_validate_json(arguments)
        except ValidationError:
            return ToolResponse(preview=self.preview, error=ToolError(code="invalid_arguments",
                message="请选择kind为规则或策略，并填写topic主题或query自然语言问题；不需要资料编号。"))
        if self.text_library is None:
            return ToolResponse(preview=self.preview, error=ToolError(code="storage_error", message="本地参考文章尚未配置。"))
        record_type = "rule" if parsed.kind == "规则" else "analysis"
        query = parsed.query or REFERENCE_TOPICS.get(parsed.topic, parsed.topic)
        response = ToolResponse(preview=self.preview)
        previous = dict(self._evidence)
        scopes = self.text_library.catalog(record_type)
        if not scopes:
            return ToolResponse(preview=self.preview, notes=[f"尚未收录{parsed.kind}资料。"])
        for scope in scopes:
            arguments = SearchArguments(record_type=record_type, scope_id=scope.scope_id, query=query, top_k=2)
            seen = {passage_key(record) for record in previous.values() if isinstance(record, TextEvidence)}
            result = self._text_query("search_records", arguments, topic=parsed.topic, exclude=seen)
            if result.error:
                self._evidence = previous
                return result
            response.results.extend(result.results)
        if not any(scope.records for scope in response.results):
            response.notes.append("本次未找到新的主题匹配片段；此前已返回的段落不重复发送。可换具体问题、补读已有资料或说明资料不足。")
        published = self._publish_text(response)
        if published.error:
            self._evidence = previous
        return published

    def resolve_citation(self, citation_id: str) -> ToolRecord | TextEvidence:
        """仅查本会话实际返回的证据；当前仍是待审预览，不能冒充正式引用。"""
        if citation_id not in self._evidence:
            raise ValueError("引用不属于本次工具会话已返回的证据")
        return self._evidence[citation_id].model_copy(deep=True)

    def _citation_id(self, identity: str) -> str:
        # 外部只需不透明编号，完整身份与定位仍保存在本会话证据中。
        digest = hashlib.sha256(f"{self._session}:{identity}".encode("utf-8")).hexdigest()[:32]
        return "preview:" + digest

    def invoke(self, name: str, arguments: str) -> ToolResponse:
        response = ToolResponse(preview=self.preview,
            notes=["当前资料待审；关联发现仅返回带来源候选，不合并技能；正式规则映射及分页未完成。"])
        if name == "get_character_skills":
            return self._get_character_skills(arguments)
        if name == "search_reference":
            return self._search_reference(arguments)
        if name == "read_evidence":
            return self._read_evidence(arguments)
        if name not in {"discover_catalog", "search_records"}:
            response.error = ToolError(code="unknown_tool", message="未登记的只读工具")
            return response
        schema = CatalogArguments if name == "discover_catalog" else SearchArguments
        try:
            parsed = schema.model_validate_json(arguments)
        except ValidationError:
            try:
                raw = json.loads(arguments)
            except ValueError:
                raw = None
            record_type = raw.get("record_type", "skill") if isinstance(raw, dict) else None
            if record_type in ("rule", "analysis"):
                message = ("rule/analysis查询不接受targets、game_ids、character、mode、skill_name、package_name、faction。"
                           "先调用discover_catalog且仅传record_type，使用实际返回的scope_id；"
                           "search_records再传record_type、scope_id、query，可选top_k=1–5。参数须为正确类型，不接受未知字段。")
            elif record_type == "skill":
                message = ("skill查询至少提供character、skill_name、package_name或faction之一；"
                           "或只传targets，每项必须有game_id、character、mode，1–10项且不重复，不混单查/文本字段。"
                           "可传game_ids、mode，省略scope_id、query、top_k、include_related。"
                           "关联仅用于discover_catalog：传character、include_related=true，可指定game_ids，不混包/势力筛选。"
                           "参数须为正确类型，不接受未知字段。")
            else:
                message = "参数须为JSON对象，record_type只允许skill、rule、analysis；不接受未知字段或错误类型。"
            response.error = ToolError(code="invalid_arguments", message=message)
            return response
        if parsed.record_type != "skill":
            return self._text_query(name, parsed)
        if isinstance(parsed, SearchArguments) and parsed.targets is not None:
            if any(target.game_id not in self.allowed_game_ids for target in parsed.targets):
                response.error = ToolError(code="scope_denied", message="批量中有目标超出允许游戏范围，整批未执行")
                return response
        game_ids = list(self.allowed_game_ids) if parsed.game_ids is None else parsed.game_ids
        if len(set(game_ids)) != len(game_ids) or any(game not in self.allowed_game_ids for game in game_ids):
            response.error = ToolError(code="scope_denied", message="查询范围重复或超出程序允许的游戏范围")
            return response
        pending_evidence: dict[str, ToolRecord] = {}
        if parsed.include_related and self.character_families is None:
            response.error = ToolError(code="scope_denied", message="程序尚未配置人物关联目录；不能猜测系列或假装已查全。")
            return response
        try:
            with KnowledgeRepository(self.database, readonly=True) as repository:
                if name == "discover_catalog":
                    families = {}
                    if parsed.include_related:
                        assert self.character_families is not None and parsed.character is not None
                        items = []
                        for game_id in game_ids:
                            item, family = self.character_families.discover(repository, game_id, parsed.character)
                            items.append(item)
                            families[game_id] = family
                    else:
                        items = repository.discover_catalog(**parsed.model_dump(exclude={"game_ids", "record_type", "include_related"}), game_ids=game_ids)
                    for item in items:
                        more = any(len(getattr(item, key)) > 20 for key in ("characters", "packages", "factions", "modes"))
                        item = item.model_copy(update={key: getattr(item, key)[:20]
                            for key in ("characters", "packages", "factions", "modes")})
                        response.results.append(ToolScopeResult(game_id=item.game_id, status="catalog_available",
                                                               catalog=item, family=families.get(item.game_id), has_more=more))
                else:
                    assert isinstance(parsed, SearchArguments)
                    queries = ([([target.game_id], QueryFilters(character=target.character, mode=target.mode), target)
                                for target in parsed.targets] if parsed.targets is not None
                               else [(game_ids, parsed.filters(), None)])
                    for query_games, filters, target in queries:
                        for item in repository.search_records(game_ids=query_games, **filters.model_dump()):
                            if not item.records:
                                status = "not_loaded" if item.status == "not_collected" else "no_match_in_loaded_data"
                                response.results.append(ToolScopeResult(game_id=item.game_id, target=target, status=status))
                                continue
                            if not self.preview:
                                response.results.append(ToolScopeResult(game_id=item.game_id, target=target, status="pending_review"))
                                continue
                            records = []
                            for record in item.records[:self.max_records]:
                                citation_id = self._citation_id("skill:" + record.record_id)
                                evidence = ToolRecord(**record.model_dump(), citation_id=citation_id)
                                records.append(evidence)
                                pending_evidence[citation_id] = evidence.model_copy(deep=True)
                            response.results.append(ToolScopeResult(game_id=item.game_id, target=target, status="preview_available",
                                records=records, has_more=len(item.records) > len(records)))
            if self.max_characters is not None and len(response.model_dump_json()) > self.max_characters:
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
        citation_id = self._citation_id("text:" + identity)
        evidence = TextEvidence(**passage.model_dump(exclude={"citation_id"}), citation_id=citation_id)
        return ToolResponse(preview=self.preview, results=[ToolScopeResult(game_id=passage.game_id,
            scope=self.text_library.collection(passage.scope_id, passage.record_type).info if self.text_library else None,
            status="preview_available", records=[evidence])])

    def _publish_text(self, response: ToolResponse) -> ToolResponse:
        if self.max_characters is not None and len(response.model_dump_json()) > self.max_characters:
            return ToolResponse(preview=self.preview, error=ToolError(code="output_limit",
                message="完整文本结果超过额度，请减少返回条数；未隐藏截断内容"))
        if response.error is not None:
            return response
        for result in response.results:
            for record in result.records:
                self._evidence[record.citation_id] = record.model_copy(deep=True)
        return response

    def _text_query(self, name: str, parsed: CatalogArguments | SearchArguments, *,
                    topic: str | None = None, exclude: set[tuple[str, str, int, int, str]] | None = None) -> ToolResponse:
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
                    limit = min(parsed.top_k, self.max_records)
                    options = {} if topic is None and exclude is None else {"topic": topic, "exclude": exclude}
                    passages = self.text_library.search(parsed.scope_id, parsed.record_type, parsed.query, limit + 1, **options)
                    records = [self._text_response(p).results[0].records[0] for p in passages[:limit]]
                    response.results.append(ToolScopeResult(game_id=collection.info.game_id, scope=collection.info,
                        status="preview_available" if records else "no_match_in_loaded_data", records=records,
                        has_more=len(passages) > limit))
        except PermissionError as error:
            response.error = ToolError(code="scope_denied", message=str(error) +
                "；先调用discover_catalog且仅指定record_type，使用该类型实际返回的scope_id，不要猜测编号。")
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
