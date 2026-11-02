# GameWise AI

本地运行、基于资料证据的游戏问答助手。首批聚焦三国杀 OL、十周年、移动版的武将系列技能；魂系保留已有成果并延后。

按 Day 记录阶段要点，不记录每轮讨论。Day 表示实施阶段，不是自然日。仅在阶段成果或关键决定变化时合并更新本页。

## Day 1：本地前后端连通

**状态：已完成，用户已验收。**

- 前端使用 React + TypeScript + Vite，后端使用 Python + FastAPI，实现网页检查 `/health` 并显示连接状态。
- 后端 pytest 2 项、前端 Vitest 8 项通过；类型检查与构建通过，用户实际验证启动、断开和恢复。尚无自动端到端测试套件。
- 完成本地 Git 分支与 Commit 练习，骨架存档为 `9979a6e`。

## Day 2：资料获取、处理与问答方向

**状态：进行中；已有样本和处理类，尚未建成知识库或 RAG。**

- **取得源数据**：通过 BWIKI MediaWiki API 读取三服候选目录，共 2,136 页，非全量技能。已处理三服孙坚及 OL 谋孙坚的小批真实样本，保留原始 JSON/HTML；魂系早期清单成果保留。
- **建立处理与存取流程**：Python 解析网页，Pydantic 校验，`DataProcessor` 整理 JSON/Markdown，SQLite 五表保存。各服读取规则复用统一输出，支持按服、模式、武将、技能、包和势力查询；重复导入不新增，错误整批回滚。同文不同模式仍分开，真实 LLM 未接入。
- **确定 ReAct + RAG 方向**：LLM 理解问题并选择受控查询工具，从知识库取得证据后回答。未指定服默认分别查三服；系列、模式分别处理，缺资料不补造。主观评价必须区分事实和观点。此方向尚未实现，ReAct 与前端 React 不同。
- **验证与限制**：阶段小样本为三服孙坚及 OL 谋孙坚，JSON/SQLite 对照和范围隔离通过。用户核实经典等标题为模式；正式规则适用期、资料使用及标签仍待审，非完整知识库。
- **下一步**：先完成三服武将全目录的采集与待审入库，再补必要规则、落实受控模型工具与问答闭环。

## Day 3：批量整理与导入

**状态：目录采集完成，首轮提取收尾完成，待用户审查。**

- 从三服小批样本扩大到冻结目录全部页面，MediaWiki API低频下载，保留原始快照、指纹与断点；HTTP 567持续退避等待，下载与解析分开统计。
- 复用各服解析、DataProcessor校验和SQLite待审导入，集中修复新式面板、图片技能名与重复显示。保持历史、台词、不同服及模式隔离，源页面空缺如实保留。
- 按武将卡思路沿用现有表和字段，已补齐原文明示获得的额外技能效果，获得条件仍读基础技能；同名状态变化及玩法混排保留缺口。具体覆盖、测试及审阅入口见PLAN，不将采集完成称为正式知识库完成。
- 武将资料进入SQLite待审库，实际覆盖见本地报告。规则56页复用DataProcessor整理成文件，已做本地E5、关键词及组合检索对照，并预演武将查询与规则补查的衔接；保留真实漏检，尚未接自主ReAct或回答LLM。

## 本地启动与检查

<details>
<summary>需要运行时展开</summary>

在项目根目录启动后端，保持终端运行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

另开终端，在 `frontend` 目录启动前端：

```powershell
npm.cmd run dev
```

访问 [本地网页](http://127.0.0.1:5173/)；后端接口为 [health](http://127.0.0.1:8000/health)。按 Ctrl+C 停止对应服务。

测试需主动执行，不会随服务启动自动运行。在 `backend` 目录：

```powershell
..\.venv\Scripts\python.exe -m pytest -q
```

在 `frontend` 目录：

```powershell
npm.cmd run typecheck
npm.cmd run test
npm.cmd run build
```

在 `backend` 目录处理本机已有的孙坚快照，不联网：

```powershell
..\.venv\Scripts\python.exe -m experiments.process_bwiki ../data/local/sgs-preview.local ../data/local/processor-preview.local
```

需要本地已有快照，预期生成 5 条待审记录。实验脚本使用模块启动；PyCharm 可选择“GameWise 处理三国杀样本”。本地真实资料位于 `data/local/*.local` 目录，不作为已提交资料或正式知识覆盖。

在 `backend` 目录使用本地 SQLite（不联网、不接 LLM）：

```powershell
..\.venv\Scripts\python.exe -m app.manage stats
..\.venv\Scripts\python.exe -m app.manage query --game sanguosha-ol --character 孙坚 --mode 1V1版本
```

数据库在 `data/local/knowledge.local/gamewise.sqlite3`，已导入现有样本。重新导入用 `..\.venv\Scripts\python.exe -m app.manage import-samples`，重复执行不新增；需要已有 characters.json 及原始快照。查询目录可用 `query --package 国战标准 --catalog`，技能反查可用 `query --skill 英魂`。结果 `answerable: false` 表示待审预览，不是报错。旧 JSON 查询保留为开发对照，日常验收使用 app.manage。

</details>

<details>
<summary>本地规则检索实验（模型已下载后使用）</summary>

在backend目录，用项目Python运行；只返回规则原文，不生成答案、不调用收费API：

```powershell
..\.venv\Scripts\python.exe -m app.rule_retrieval query "失去体力" --top-k 5
..\.venv\Scripts\python.exe -m app.rule_retrieval query "失去体力" --top-k 5 --method keyword
..\.venv\Scripts\python.exe -m app.rule_retrieval evaluate
..\.venv\Scripts\python.exe -m app.rule_retrieval --index-dir ../data/local/embedding-preview.local/index-v2 query "失去体力和受伤的区别" --method hybrid --top-k 5
```

模型、索引和逐题报告在data/local/embedding-preview.local/。新环境先按backend/requirements-embedding.txt安装可选CPU依赖；模型下载需按已确认方案显式准备，查询不会自动下载。当前是开发实验，分数不是正确概率，范围外问题可能误召回，未完成正式问答验收。

</details>

## 文档分工

| 文档 | 只记录什么 |
| --- | --- |
| [PLAN.md](PLAN.md) | 当前进度、下一步、14 阶段安排 |
| [PRD.md](docs/PRD.md) | 产品需求与验收边界 |
| [PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) | 长期背景及关键决策 |
| [AGENTS.md](AGENTS.md) | 开发与合作规则 |
| [DATA_DESIGN.md](docs/DATA_DESIGN.md) | 按需查阅的字段、关系及工具设计草案 |
