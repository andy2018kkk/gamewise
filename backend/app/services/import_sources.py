"""校验本地资料清单与文件指纹，复用来源表登记长文或摘要。"""

import hashlib
from pathlib import Path

from app.repositories.knowledge import ImportReport, KnowledgeRepository
from app.schemas.query import Game
from app.schemas.source import SourceBatch


def import_source_files(database: Path, manifests: list[Path], games: list[Game]) -> ImportReport:
    """整批预检后原子入库；正文留在文件中，适用版本未知时保持待审。"""
    known_games = {game.game_id for game in games}
    if not manifests or not games or len(known_games) != len(games):
        raise ValueError("来源清单与不重复的游戏登记不能为空")
    sources = []
    for manifest in manifests:
        batch = SourceBatch.model_validate_json(manifest.read_text(encoding="utf-8-sig"))
        for source in batch.sources:
            if source.game_id not in known_games or source.review_status != "pending":
                raise ValueError("资料须属于登记范围且保持待审")
            if not source.content_hash:
                raise ValueError("资料缺少文件内容指纹")
            path = Path(source.content_path)
            path = path if path.is_absolute() else manifest.parent / path
            if hashlib.sha256(path.read_bytes()).hexdigest() != source.content_hash:
                raise ValueError("资料文件内容指纹不一致")
            source.content_path = str(path.resolve())
            sources.append(source)
    batch = SourceBatch(sources=sources)
    if not batch.sources:
        raise ValueError("没有可登记资料")
    with KnowledgeRepository(database, initialize=True) as repository:
        return repository.import_sources(games, batch.sources)
