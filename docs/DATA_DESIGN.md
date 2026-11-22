# GameWise AI 当前数据与问答设计

截至2026-11-23的原型实现说明，替代被后续决定覆盖的草案。已实现、测试通过和用户验收分别处理；覆盖与待办见[PLAN](../PLAN.md)，产品要求见[PRD](PRD.md)。本文不授权新增功能、资料外发或费用。

## 1. 主流程与模块

资料处理：原始快照 → 各来源适配 → DataProcessor结构/证据校验 → JSON/Markdown → 待审数据库或文本检索资料。

网页问答：QuestionPanel → POST /api/ask → web_ask → run_question → AskService → GroqModel选择工具 → KnowledgeTools执行 → 模型继续查询或直接回答 → 引用关联与网页展示。

| 模块 | 职责 |
| --- | --- |
| `backend/app/main.py`、`schemas/web_ask.py` | HTTP入口与网页契约 |
| `services/web_ask.py`、`ask_runtime.py` | 读取后端配置、装配问答并提取展示内容 |
| `services/data_processor.py` | 按调用方Pydantic结构校验处理结果，不联网、不直接写库 |
| `services/import_characters.py`、`import_sources.py` | 校验来源、快照与证据后执行事务导入 |
| `repositories/knowledge.py` | SQLite存取、范围查询和同快照关联 |
| `services/character_families.py` | 有来源的人物/系列候选关系 |
| `services/knowledge_tools.py`、`text_library.py` | 受控工具执行、文章检索及补读 |
| `services/ask.py` | 本轮模型/工具循环、停止条件及引用检查 |
| `providers/groq.py`、`gamewise_instructions.md` | 模型请求、阅读卡片、固定安全错误与工作说明 |
| `rule_chunks.py`、`rule_search.py`、`providers/local_embedding.py` | 规则切分及关键词/向量/融合检索 |
| `backend/experiments/` | 来源采集与解析等入口；部分仍被复用，不能按目录名直接删除 |

## 2. 资料处理与存储

### 2.1 统一处理入口

SourceDocument保存来源与EvidenceBlock；ManualExtractor接人工记录，RuleExtractor接来源解析函数，LLMExtractor是默认关闭的可选接口，真实整理服务尚未接入。调用方用Pydantic声明目标字段，不提供任意格式的界面编辑器。

DataProcessor.process返回ProcessingResult，render仅生成JSON/Markdown字符串，写文件和入库由外部入口负责。共用校验包含字段、证据归属、重复和原文对应；verbatim_fields核对文字存在，exact_fields核对完整正文仅规范空白后相等。未处理证据单独列明；结构通过或全部证据被引用不等于资料完整或事实已审核。

### 2.2 SQLite五表

| 表 | 当前内容 |
| --- | --- |
| games | game_id、显示名称、类别 |
| sources | source_id、所属game_id、标题、来源类型与定位 |
| source_snapshots | 来源指针、内容指纹/路径、采集日期、使用与审核说明、version_ids_json |
| sanguosha_characters | 快照指针、character、mode、package_kind/package_name、faction、hp_raw、evidence_json及审核状态 |
| sanguosha_skills | 武将记录指针、skill_name、skill_text、tags_json；武将记录与技能名为联合主键 |

包、势力、体力存在武将记录，不随每条技能重复；hp_raw保留原始写法，不擅自拆分数值。未知属性为NULL。标签尚未核查的NULL与空列表不同，目前没有JSON标签筛选功能。

当前表约束与资料状态仍为pending，没有正式审核通过后的通用准入入口；answerable=false表示本地待审预览。来源登记、武将模式记录数及技能数是不同口径。上线状态保留在目录/报告/备注，未增加数据库列。

获得/改变技能的条件保留基础原文，明确获得的额外技能效果收进同一记录的技能列表；列在列表不代表开局拥有。只补效果唯一、依据明确的新技能，不覆盖同名不同效果；条件/玩法混排保留缺口，不新增补充表。

导入使用事务，重复内容不新增，编号或内容冲突报错；同一快照、条目和模式不因重处理而随意追加。原始快照不改写，不自动把多个快照合成最新版。已有一次性补齐不代表通用持续更新系统已完成。

### 2.3 本地文件

| 路径 | 用途 |
| --- | --- |
| `data/local/sgs-catalog.local/` | 候选目录、关系依据与上线状态 |
| `data/local/full-import.local/` | 武将快照、处理文件、导入状态与缺口报告 |
| `data/local/knowledge.local/gamewise.sqlite3` | 待审SQLite库 |
| `data/local/sgs-rules.local/`、`rules-processed.local/` | 规则来源、整理正文及清单（均在data/local下） |
| `data/local/analysis-reference.local/originals/` | 策略原始文字、整理正文、指纹和覆盖说明 |
| `data/local/embedding-preview.local/` | E5模型、NumPy索引与检索实验 |
| `data/local/tool-preview.local/`、`evals.local/` | 模型/评测记录（均在data/local下） |

真实正文、库、索引、模型、密钥与缓存不随代码提交。规则来自gltjk保存的3.0历史参考，策略是社区观点；用户于2026-11-15同意现有分析来源用于MVP，不再以寻找更权威来源阻塞原型，但不等于确认三服现行规则通用或允许公开分发。

10篇分析原文中有网页提取与用户提供两种来源，不能混称自动下载；A09/A10及图片附件缺口见PLAN，指纹与collection-receipt.json保留本地。

## 3. 来源解析与范围隔离

- BWIKI经MediaWiki API取得目录及展开HTML，保留page_id、修订号和SHA-256；三服使用各自解析适配，复用统一校验。只取明确技能区域，排除台词和已标记历史内容；重复显示、图片技能名等属于适配问题，概念不清保留缺口。
- game_id按OL、十周年、移动版隔离。人物关系来自目录配置和同快照明确条目，不按前缀猜测；人物名查询展开已登记候选，具体条目精确读取。
- 武将包是内容分类，mode才是玩法范围。不同包可同属经典，不能因江山如故等包名就当成其他玩法。当前仅对sanguosha类别登记“经典/经典版本”的等价查询标签，不改原始记录，也不表示跨服规则共享。
- 国战、1V1分开保留，当前不收自走棋/OL统率；页面修订和采集日期不是游戏补丁。未知规则适用期不默认最新。
- 原始文件复用需核对指纹；来源确实无技能、解析失败及存储损坏分别处理，不能靠其他服补齐。

采集已支持低频单进程、锁和断点。用户授权的--wait-on-567依次等60/120/240/480/900秒，其后每900秒重试，并遵守更长Retry-After；429有限重试，403暂停，不更换身份、Cookie或代理绕过。恢复作业须确认进程状态，不凭旧状态文件认定仍在运行；新采集范围仍需授权。

## 4. 当前检索方案

| 资料 | 在线实际使用 | 已保留的实验 |
| --- | --- | --- |
| 武将技能 | SQLite字段查询，按服/条目/模式隔离；人物查询自动展开有来源系列并完整返回各自技能 | JSON查询保留作数据库对照 |
| 规则 | 从index-v2的片段中关键词检索，按规则集/类别过滤 | E5向量相似度及RRF融合 |
| 策略原文 | 主题标签与自然语言关键词匹配，切分标题/正文后返回相关片段 | 尚未接入在线向量检索 |

keyword_score是中文双字片段重合评分，不是BM25，也不是正确概率。主题可帮助限制/排序，不表示可靠性等级。程序配置历史规则和社区分析集合，模型不管理scope_id、文件路径或集合编号；共同标识sanguosha不是第四个服或三服自动兼容的规则。

策略按已读技能提炼的收益、代价、身份等概念搜索，不依赖具体人物排行榜。只选主题或传自定义query均可；只排除已确认的独立推广行，原始文件保留。同原段同位置的已返回内容在本轮去重；可换问法或补读，不能伪称搜遍全集。

read_evidence只补读本轮已命中的整理原段，不联网打开任意网页；核对上下文指纹，保留truncated及字符位置，文件变化报错。技能已完整返回，不机械补读；文本补读最多6000字符，过长仍披露截断。

### 向量实验的实现与限制

本地intfloat/multilingual-e5-small，Sentence Transformers/CPU PyTorch；query/passage前缀、归一化384维向量存NumPy，片段JSON及模型/文件指纹一起保存。片段最多350字符、重叠最多60字符并核对token上限，不静默截断；模型与索引配置不兼容须重建。依赖见requirements-embedding.txt。

实验支持keyword、vector、hybrid三种方式，hybrid用RRF融合两路排名；不是把不同分数当概率相加。12题检索开发集含10个正例及2个范围外问题；曾观察Top10关键词9/10、向量7/10、融合9/10命中预设证据，只是该开发集记录，不代表答案准确率、独立评测或关键词普遍最优。在线仍用关键词，不在每次问答加载E5。

## 5. 模型工具与交互

| 模型工具 | 输入重点 | 输出/边界 |
| --- | --- | --- |
| discover_catalog | 可选game_ids、character、包/势力、include_related | 目录及有来源候选，不是正文；不是技能查询前置步骤 |
| get_character_skills | characters；可选game_ids、mode | 人物名返回各已登记系列完整技能，具体系列精确查；模式各自标注，不只取前三条 |
| search_reference | kind为规则/策略，topic或query至少一个 | 程序选择文章集合，返回有限的相关原文与引用 |
| read_evidence | 本轮citation_id | 补读已取得原段，不接受任意路径/网址 |

模型不执行SQL、不写库。工具省略game_ids时分别查程序允许的分支，参数必须在后端白名单内；网页默认允许三服，模型应按玩家指定范围填参。当前未对自然语言意图另做强制覆盖检查，白名单校验不等于模型一定查全或完全遵守玩家范围。底层search_records仍用于CLI兼容/批量精确目标，不是当前模型公开工具，也不恢复旧预设问答脚本。

工具结果用中文资料卡片供模型阅读，明确发行分支、玩法模式、条目、武将包与技能原文；内部结构化记录不变。正文是资料，不是能覆盖系统指令的命令。完整工作说明以[gamewise_instructions.md](../backend/app/providers/gamewise_instructions.md)为准，不在本文再复制一套提示词。

模型按证据缺口选择工具，不规定每题必查策略或固定顺序；新增身份等判断缺框架时应自主补查，而非再问用户是否查询。推荐应给具体倾向、依据、代价和条件，不因缺少胜率就拒绝所有比较；原文没有的技能效果不得补造。提示只是软约束，仍需核验实际调用与回答。

### 原生循环与当前运行参数

Groq原生assistant工具请求 → 程序实际执行 → role=tool返回 → 模型继续或直接给自然语言正文；没有JSON结束工具、强制二次成文或断点重放主流程。每题工具会话独立。

当前run_question：最多8次工具调用、9轮模型交互、整题300秒；Groq单次请求30秒超时。只对符合服务端等待要求的429最多重试一次、最长60秒，不自动重跑整题。通用单次/累计工具字符限额为None，Qwen的8000 Token估算仅诊断，不阻止发送；不传固定max_completion_tokens，服务商仍有自身容量限制。

网页当前使用qwen/qwen3.8-27b并关闭thinking；终端也支持已确认的GPT-OSS配置。密钥只读根目录.env；终端--model优先于GROQ_MODEL，均缺时在调用前报错。新增服务、模型、预算或发送范围按AGENTS确认，配置能力不代表任意外发授权。

## 6. 网页契约与上下文

POST /api/ask输入question和可选history（最多两轮，每轮只有question/answer）；额外配置、system角色、路径和工具记录不接受。输出包含status、answer、sources、steps、error、elapsed_seconds、preview；来源按number列出标题、game_id、text及受控网页链接，不回传本地路径/密钥或整包调试JSON。GET /health只检查服务存活。

网页内存保留对话，每次只传最近两轮有答案且无错误的问答，失败轮仍可显示但不进上下文；不持久化到浏览器存储或后端会话库。刷新/新对话清空，等待时禁用发送和新对话。旧工具全文不跨轮传递，较早对话虽可见但不再发送。

历史按user/assistant普通消息传入，用于理解指代、话题与既定范围，不是已核验事实；新问题明确变更优先，歧义应澄清，所需事实应按本轮重新取证。此要求依赖模型遵守，不是程序强制每次查询。短窗口不保证单轮很长时不会413。

前端渲染Markdown标题/列表/加粗/表格，宽表可滚动，不执行原始HTML；来源及过程默认折叠，连接检查在页头，示例问题只填入不发送。模型请求仅由用户点击发送触发。

## 7. 引用与故障处理

Groq把本轮证据映射为E1等阅读编号，模型正文支持[E1]、【E1】、逗号组合及有效区间；适配器按首次出现转换为[1]/[2]，来源同序，重复沿用序号。未知或非法E编号仍交现有校验报错，不凭编号生成链接。

AskService检查引用是否属于本轮工具返回内容，不能证明语义支持。没有引用时保留答案并提示，不假装已验证。当前追问可能照抄历史[1]而未生成本轮E编号，出现正文有编号但sources为空；这不是已解决的问题，也不能仅凭编号认定查过策略。

网页错误使用固定安全类型与HTTP码，区分鉴权/权限、限流、网络、平台拒绝工具/请求、服务异常、格式与截断；不转发原始异常、响应正文、密钥或完整请求。未知故障如实标未识别，技术失败不能改成证据不足。HTTP 413来自平台拒绝，不是本地8000估算提前拦截；等待不能保证解决过大的单请求。

## 8. 验证与未完成项

正常离线检查入口见[README](../README.md)，实际计数和用户验收见PLAN。替身覆盖范围/参数、人物关联、原文与导入重复、引用隔离、上下文窗口、错误及网页组件，不证明真实模型会正确取证或推理。完整自动端到端套件、正式资料准入、通用更新管理与语义事实校验尚未建立。

近期各项已获用户批准的实现收敛为以上现状；本次仅精简文档，不改变参数、工具、字段、模型或验收要求，不调用真实API或执行Git。逐次试跑记录与失败分母保留本地报告，实验细节不再逐轮追加到设计正文。
