"""校验本地武将处理结果，再交由 SQLite 事务保存。"""

import hashlib
from pathlib import Path

from app.repositories.knowledge import ImportReport, KnowledgeRepository
from app.schemas.character import CharacterRecord
from app.schemas.query import Game
from app.services.data_processor import ProcessingResult


def _validate_batch(batch: ProcessingResult[CharacterRecord], path: Path, known_games: set[str]) -> None:
    source = batch.source
    if source.game_id not in known_games:
        raise ValueError("资料所属游戏未登记")
    if source.review_status != "pending" or batch.review_status != "pending":
        raise ValueError("当前入口只导入待审资料，不能自动批准知识库")
    if not source.content_hash:
        raise ValueError("入库前必须提供原始快照内容指纹")
    raw_path = Path(source.content_path)
    if not raw_path.is_absolute():
        raw_path = path.parent / raw_path
    if hashlib.sha256(raw_path.read_bytes()).hexdigest() != source.content_hash:
        raise ValueError("原始快照内容指纹不一致")
    # 保存绝对位置，避免更换命令工作目录后无法回溯。
    source.content_path = str(raw_path.resolve())
    seen = set()
    for record in batch.records:
        if not record.record_id.strip() or record.record_id in seen:
            raise ValueError("记录编号为空或同批重复")
        seen.add(record.record_id)
        if not record.evidence or len({block.block_id for block in record.evidence}) != len(record.evidence):
            raise ValueError("记录证据缺失或重复")
        for skill in record.data.skills:
            body = " ".join(skill.skill_text.split())
            if not any(body == " ".join((block.verbatim_text or block.text).split()) for block in record.evidence):
                raise ValueError("技能原文与完整证据不符")
        texts = [" ".join(block.text.split()) for block in record.evidence]
        for name in ("character", "mode", "package_name", "package_kind", "faction", "hp_raw"):
            value = getattr(record.data, name)
            if value is not None and not any(" ".join(value.split()) in text for text in texts):
                raise ValueError(f"属性 {name} 缺少原文依据，保持待核查")


def import_character_files(database: Path, paths: list[Path], games: list[Game]) -> ImportReport:
    """整批预检后再开库；预检与事务错误都向上报告，不静默漏收。"""
    known_games = {game.game_id for game in games}
    if not games or len(known_games) != len(games) or not paths:
        raise ValueError("游戏登记和输入文件不能为空，游戏编号不能重复")
    batches = []
    for path in paths:
        batch = ProcessingResult[CharacterRecord].model_validate_json(path.read_text(encoding="utf-8"))
        _validate_batch(batch, path, known_games)
        batches.append(batch)
    with KnowledgeRepository(database, initialize=True) as repository:
        return repository.import_batches(games, batches)
