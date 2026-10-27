"""创建本地后端应用。"""

from fastapi import FastAPI

app = FastAPI(title="GameWise AI")


@app.get("/health")
def health() -> dict[str, str]:
    """返回服务存活状态，不检查模型或知识库。"""
    return {"status": "ok"}
