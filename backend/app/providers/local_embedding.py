"""已确认的 E5 本地模型适配；正常运行只读取本地文件，不隐式下载。"""

from pathlib import Path
import json

MODEL_ID = "intfloat/multilingual-e5-small"


class LocalE5Embedding:
    def __init__(self, model_dir: Path) -> None:
        if not (model_dir / "model.safetensors").is_file():
            raise FileNotFoundError("本地E5模型未准备完成，请先显式下载")
        self.identity = json.loads((model_dir / "gamewise-model.json").read_text(encoding="utf-8"))
        if self.identity["model_id"] != MODEL_ID:
            raise ValueError("模型不是已确认的E5模型")
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(4)
        self.model = SentenceTransformer(str(model_dir), device="cpu", local_files_only=True,
                                         trust_remote_code=False, model_kwargs={"use_safetensors": True})
        self.model.max_seq_length = 512

    def count_passage(self, text: str) -> int:
        return len(self.model.tokenizer("passage: " + text, truncation=False)["input_ids"])

    def encode(self, texts: list[str], *, query: bool = False):
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("向量输入不能为空")
        prefix = "query: " if query else "passage: "
        inputs = [prefix + text for text in texts]
        if any(len(self.model.tokenizer(text, truncation=False)["input_ids"]) > 512 for text in inputs):
            raise ValueError("输入超过512 tokens，请先切分，不能静默截断")
        return self.model.encode(inputs, batch_size=16, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=not query)
