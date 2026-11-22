"""创建本地后端应用。"""

from fastapi import FastAPI

from app.schemas.web_ask import WebAnswer, WebQuestion
from app.services.web_ask import answer_web_question

app = FastAPI(title="GameWise AI")


@app.get("/health")
def health() -> dict[str, str]:
    """返回服务存活状态，不检查模型或知识库。"""
    return {"status": "ok"}


@app.post("/api/ask", response_model=WebAnswer)
async def ask(request: WebQuestion) -> WebAnswer:
    """处理当前问题与有限历史，模型与资料配置仅由后端控制。"""
    return await answer_web_question(request.question, history=tuple(request.history))
