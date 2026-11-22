"""读取已有消歧义目录的来源分组；发现候选，不猜前缀、不合并技能正文。"""

from dataclasses import dataclass
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.repositories.knowledge import KnowledgeRepository
from app.schemas.query import CatalogItem
from app.schemas.query import SamePageRelation
from app.schemas.tools import FamilyCandidate, FamilyDiscovery, RelationSource


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_review: str
    branch_game_ids: dict[str, str]


@dataclass
class _Group:
    members: tuple[str, ...]
    source: RelationSource
    conflict: bool = False


class CharacterFamilies:
    """按发行分支和人物精确定位有来源的关联分组。"""

    def __init__(self) -> None:
        self._groups: dict[tuple[str, str], _Group] = {}

    @classmethod
    def from_config(cls, config_path: Path) -> "CharacterFamilies":
        config = _Config.model_validate_json(config_path.read_text(encoding="utf-8-sig"))
        review_path = (config_path.parent / config.catalog_review).resolve()
        data = json.loads(review_path.read_text(encoding="utf-8"))
        result = cls()
        for branch in data["branches"]:
            game_id = config.branch_game_ids.get(branch["branch"])
            if game_id is None:
                continue
            raw_path = (review_path.parent / (branch["branch"] + "-relations.json")).resolve()
            if not raw_path.is_relative_to(review_path.parent):
                raise ValueError("目录快照超出配置目录")
            raw = json.loads(raw_path.read_text(encoding="utf-8"))["parse"]
            lines = raw["wikitext"]["*"].splitlines()
            for group in branch["groups"]:
                line = group["source_line"]
                if (type(line) is not int or not 1 <= line <= len(lines)
                        or group["source_revision"] != raw["revid"] or group["source_raw"] != lines[line - 1]):
                    raise ValueError("目录分组与原始行或修订号不一致")
                titles = group["titles"]
                match = re.fullmatch(r"\s*\{\{消歧义\|([^|{}]+)\|([^|{}]+)\}\}\s*", group["source_raw"])
                if (not match or "<!--" in group["source_raw"] or "^" in match[2]
                        or match[1].strip() != group["label"]
                        or [name.strip() for name in match[2].split("、")] != titles):
                    raise ValueError("目录成员与受支持的原始关联行不一致")
                if not isinstance(titles, list) or not titles or any(not isinstance(name, str) or not name.strip() for name in titles):
                    raise ValueError("目录分组成员无效")
                members = tuple(dict.fromkeys(titles))
                source = RelationSource(source_url=group["source_url"], revision=raw["revid"], lines=[line])
                key = (game_id, group["label"])
                if key in result._groups:
                    previous = result._groups[key]
                    previous.conflict |= (set(previous.members) != set(members) or
                        previous.source.source_url != source.source_url or previous.source.revision != source.revision)
                    previous.source.lines = sorted(set(previous.source.lines + [line]))
                else:
                    result._groups[key] = _Group(members, source)
        return result

    def discover(self, repository: KnowledgeRepository, game_id: str, person: str) -> tuple[CatalogItem, FamilyDiscovery]:
        exact = repository.discover_catalog(game_ids=[game_id], character=person)[0]
        group = self._groups.get((game_id, person))
        family = FamilyDiscovery(person=person, status="not_registered")
        if group is None:
            return exact, family
        family.source = group.source.model_copy(deep=True)
        if group.conflict:
            family.status = "conflict"
            return exact, family
        family.status = "registered"
        supplements: dict[str, list[SamePageRelation]] = {}
        for relation in repository.same_page_relations(game_id, list(group.members)):
            if relation.character not in group.members:
                supplements.setdefault(relation.character, []).append(relation)
        members = (*group.members, *sorted(supplements))
        catalogs = []
        for member in members:
            item = repository.discover_catalog(game_ids=[game_id], character=member)[0]
            catalogs.append(item)
            family.candidates.append(FamilyCandidate(character=member, modes=item.modes, packages=item.packages,
                skill_count=item.record_count, coverage="loaded" if item.record_count else "not_loaded",
                same_page_evidence=supplements.get(member, [])))
        return exact.model_copy(update={
            "characters": sorted({c for item in catalogs for c in item.characters}),
            "modes": sorted({m for item in catalogs for m in item.modes}),
            "packages": sorted({p for item in catalogs for p in item.packages}),
            "factions": sorted({f for item in catalogs for f in item.factions}),
            "record_count": sum(item.record_count for item in catalogs),
            "status": "sample_available" if any(item.record_count for item in catalogs) else exact.status,
        }), family
