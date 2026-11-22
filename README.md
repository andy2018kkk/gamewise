# GameWise AI

面向多游戏、多版本的本地 AI 攻略助手，围绕技能查询、机制解释和版本比较提供带来源的回答。首批 MVP 以三国杀 OL、十周年、移动版验证流程；其他游戏尚未接入当前问答。

**当前为可运行原型。** 仓库提供代码、配置示例及合成测试，不包含密钥、真实资料正文、SQLite 数据库、向量索引或模型文件。下载代码后需另行准备本地资料，才能体验实际游戏问答。

## 核心功能

- **资料处理：** 统一处理入口与各来源解析适配，支持结构校验、快照留存和重复导入检查。
- **按范围查询：** 按发行分支、武将条目与玩法模式查技能，武将包与模式分别记录。
- **工具调用问答：** 模型自主选择目录发现、技能查询、规则/策略检索或原文补读，程序负责受控执行。
- **网页交互：** 支持 Markdown、来源原文和查询过程展示；简短追问携带最近两轮成功问答，刷新或“新对话”清空记录。

## 技术方案

| 部分 | 当前实现 |
| --- | --- |
| 前端 | React、TypeScript、Vite；react-markdown、remark-gfm 排版 |
| 后端 | Python、FastAPI、Pydantic |
| 技能资料 | SQLite 结构化查询，保留来源和快照归属 |
| 规则与策略 | 主题/关键词检索，按需返回原文片段 |
| 模型调用 | Groq API、原生 Tool Calling；网页当前使用 Qwen |
| 自动测试 | pytest、Vitest、模型与 HTTP 测试替身 |

问答主链路：网页 → FastAPI → 问答循环 → 模型选择工具 → 本地资料查询 → 模型回答 → 答案与来源展示。

本地多语言 E5 向量检索、关键词/向量 RRF 融合已开展小规模对照实验，**尚未接入网页主流程**；未证明某种检索方式对所有问题最优。

## 本地运行

以下命令适用于 Windows PowerShell。需要 Python、Node.js/npm；完整的新环境恢复安装尚未验证。

### 1. 准备环境与配置

在项目根目录按需准备依赖；已有环境不必重复创建：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm.cmd --prefix frontend ci
```

参照 [.env.example](.env.example) 在根目录配置 `.env`，填写 `GROQ_API_KEY`；已有密钥文件不要覆盖或提交。网页当前指定 `qwen/qwen3.8-27b`，终端入口则支持 `--model` 优先于 `.env` 的 `GROQ_MODEL`。

实际问答还需本地数据库 `data/local/knowledge.local/gamewise.sqlite3` 及下列配置所指向的资料：

- [text-collections.example.json](backend/text-collections.example.json)：规则索引、策略原文及主题映射。
- [character-families.example.json](backend/character-families.example.json)：人物与系列关联。

这些是本地路径配置，不是随仓库附送的知识库；资料处理方式见 [数据设计](docs/DATA_DESIGN.md)。

### 2. 启动服务

在项目根目录启动后端，保持终端运行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

另开终端，在项目根目录启动前端：

```powershell
npm.cmd --prefix frontend run dev
```

打开 [本地网页](http://127.0.0.1:5173/)。只有点击发送才会调用模型，将问题、最近两轮成功问答及本次查询资料发送至 Groq，消耗账号额度，可能产生费用。按 Ctrl+C 停止对应服务。

接口：`GET /health` 检查服务存活，`POST /api/ask` 接收问题与最多两轮历史文字；密钥和资料路径仅由后端读取。

## 测试与其他入口

测试须主动执行，不会随启动自动运行，默认使用替身、不调用真实模型。规则索引相关测试还需要已安装 NumPy（版本见可选依赖文件）；不需要为普通接口测试下载 embedding 模型。

在 `backend` 目录运行后端检查：

```powershell
..\.venv\Scripts\python.exe -m pytest -q
```

在 `frontend` 目录运行前端检查：

```powershell
npm.cmd run typecheck
npm.cmd run test
npm.cmd run build
```

<details>
<summary>终端问答、数据库查询与检索实验</summary>

以下从 `backend` 目录运行。终端问答会调用外部模型，同样需要密钥、本地资料及已确认的使用范围：

```powershell
..\.venv\Scripts\python.exe -m app.groq_preview --run-remote --model qwen/qwen3.8-27b --game sanguosha-ol --question "你的问题"
```

只读查看本地数据库，不调用模型：

```powershell
..\.venv\Scripts\python.exe -m app.manage stats
..\.venv\Scripts\python.exe -m app.manage query --game sanguosha-ol --character 孙坚 --mode 1V1版本
```

可选规则检索实验需要 [额外依赖](backend/requirements-embedding.txt)、本地模型及索引；查询不会自动下载模型：

```powershell
..\.venv\Scripts\python.exe -m app.rule_retrieval --index-dir ../data/local/embedding-preview.local/index-v2 query "失去体力和受伤的区别" --method hybrid --top-k 5
```

</details>

## 已知限制

- 真实资料仍为待审预览，不代表三服全量覆盖、线上最新或已完成正式资料准入；历史规则和社区观点不自动适用于所有服。
- 模型仍可能曲解技能、遗漏系列或跳过必要查询；引用归属检查不等于语义事实校验，追问中的引用关联也可能不完整。
- 平台限流（HTTP 429）和请求过大（HTTP 413）可能中断问答；两轮上下文窗口不保证长内容不会超限。
- 自动测试验证程序行为，不等于真实模型效果或完整端到端验收；现有探索性对照不能支撑准确率提升的量化结论。

## 项目结构与文档

```text
frontend/src/             问答页面、请求方法与展示组件
backend/app/              API、问答装配、资料处理、检索、模型适配与存储
backend/tests/            离线自动测试与合成资料
backend/experiments/      采集解析与历史实验，部分仍被现有入口复用
data/local/               本机资料、数据库、索引、模型与报告，不随代码提交
evals/datasets/           检索开发题集
```

[产品需求](docs/PRD.md) · [数据与工具设计](docs/DATA_DESIGN.md) · [长期背景](docs/PROJECT_CONTEXT.md) · [计划与进度](PLAN.md) · [开发规范](AGENTS.md)
