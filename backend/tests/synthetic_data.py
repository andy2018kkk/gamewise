"""仅供离线测试使用的虚构数据库，不进入应用运行入口。"""
from datetime import date
import hashlib
from pathlib import Path
from app.schemas.character import CharacterRecord
from app.schemas.query import Game
from app.schemas.source import SourceRecord
from app.services.data_processor import DataProcessor, EvidenceBlock, ManualExtractor, SourceDocument
from app.services.import_characters import import_character_files

def build_synthetic_database(root: Path) -> Path:
    """复用正式校验/导入入口，在临时库放入一份明确虚构的技能。"""
    text = "准备阶段，你可以弃置1张手牌，然后摸3张牌；每回合限一次。"
    meta = "测试武将甲 测试经典 武将包 测试包 吴"
    raw = root / "synthetic.txt"
    raw.write_text(meta + "\n试学：" + text, encoding="utf-8")
    source = SourceRecord(source_id="synthetic-source", title="人工构造工具调用样本", game_id="synthetic-a",
        topic="synthetic", source_type="local", source_locator="synthetic:example-1", content_path=str(raw),
        collected_at=date.today(), content_hash=hashlib.sha256(raw.read_bytes()).hexdigest(),
        usage_note="项目人工构造内容，仅用于API流程实验，不代表游戏事实")
    document = SourceDocument(source=source, blocks=(
        EvidenceBlock(block_id="meta", text=meta, locator="第1行"),
        EvidenceBlock(block_id="skill", text=text, verbatim_text=text, locator="第2行")))
    data = CharacterRecord(character="测试武将甲", mode="测试经典", package_kind="武将包",
        package_name="测试包", faction="吴", skills=[{"skill_name": "试学", "skill_text": text}])
    batch = DataProcessor(CharacterRecord).process(document, ManualExtractor([
        {"data": data.model_dump(), "block_ids": ["meta", "skill"]}]))
    processed = root / "synthetic.json"
    processed.write_text(batch.model_dump_json(), encoding="utf-8")
    database = root / "synthetic.sqlite3"
    import_character_files(database, [processed], [Game(game_id="synthetic-a", name="人工构造测试服")])
    return database
