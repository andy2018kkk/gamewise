# GameWise AI 本地开发说明

GameWise AI 是面向普通玩家的多游戏、多版本、基于证据的攻略助手。目前已实现后端健康接口和本地连接状态页面，游戏知识库与 AI 问答尚未接入。页面配色和排版仅为当前骨架，最终美术风格与图片以后单独确认。

## 本次功能怎样工作

浏览器访问 `/health`，FastAPI 根据地址找到 `health()` 函数，把返回的 Python 字典转换成 JSON 响应。Uvicorn 负责运行应用并接收本机请求。

- `backend/app/main.py`：后端入口与健康接口。
- `backend/tests/test_health.py`：模拟请求，检查响应状态、内容及不支持的请求方法。
- `backend/requirements.txt`：本次验证的直接依赖版本。
- `frontend/src/App.tsx`：页面状态和重新检查按钮。
- `frontend/src/api/health.ts`：请求健康接口并检查响应。
- `frontend/vite.config.ts`：本地前端服务及健康请求转发配置。
- `frontend/package.json`、`package-lock.json`：前端命令、直接依赖版本及安装锁定记录。
- `PLAN.md`：实际进度、测试结果和待验收项。

## 开发环境

本次已在 Windows、Python 3.14.4 和项目 `.venv` 中验证。环境文件不提交到仓库；其他 Python 版本及全新环境尚未验证。下面的命令在 PowerShell 中执行，前两节从项目根目录 `C:\Users\user\Desktop\gamewise` 开始。

无需激活环境，直接调用 `.venv` 中的 Python 可以避免误用其他项目的工具。

## 安装已确认的依赖

当前电脑的项目环境已完成安装，无需重复执行。将来恢复环境时，可在已有 `.venv` 的前提下执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

FastAPI 定义接口，Uvicorn 启动服务，pytest 运行测试，HTTPX 为测试客户端提供请求能力。Pydantic 等必要间接依赖随 FastAPI 自动安装。依赖清单固定直接依赖版本，并非所有间接依赖的完整锁定文件。

## 启动后端

在项目根目录的终端运行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

保持终端运行，在浏览器打开 [健康接口](http://127.0.0.1:8000/health)。预期看到：

```json
{"status":"ok"}
```

`127.0.0.1` 表示本机，这个服务不面向外网。`app.main:app` 表示加载 `app/main.py` 中名为 `app` 的应用，`--app-dir backend` 指定代码所在目录。

在运行服务的终端按 `Ctrl+C` 停止。浏览器再次访问或刷新时应无法连接；重新运行命令后刷新，应恢复正常。若端口被其他程序占用，不要结束不明进程；先报告错误，或临时换一个端口并同步访问地址。

访问 `/` 显示 `Not Found` 是预期行为，因为本次没有实现首页。`/health` 正常仅证明本地后端可以响应，不证明知识库、模型或网页已可用。

## 运行自动测试

在项目根目录的另一个终端执行：

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

预期两项测试通过：GET 请求返回 HTTP 200 和准确的 JSON；POST 请求返回 HTTP 405，即该地址不接受提交数据。测试使用应用内客户端，不要求提前启动 Uvicorn，也不调用外部模型。

当前 Starlette 的 TestClient 对 HTTPX 有弃用提示，测试仍可运行；本次保留已获准的依赖，不增加 httpx2。未来升级测试依赖时单独处理。这项提示不是测试失败。

## 启动前端页面

先保持后端终端运行，再打开一个新的 PowerShell 终端：

```powershell
cd C:\Users\user\Desktop\gamewise\frontend
npm.cmd run dev
```

浏览器打开 [GameWise 页面](http://127.0.0.1:5173/)。这是前端首页，后端健康接口仍在 8000 端口。两个终端都要保持运行，停止时在对应终端按 `Ctrl+C`。若 5173 已被占用，Vite 会明确报错而不自动换端口，避免访问错服务。

页面打开时自动检查一次；后端状态变化后点击“重新检查”。检查期间按钮禁用；请求 5 秒未完成则取消并提示无法连接。只有 HTTP 请求成功且响应包含 `status: "ok"` 时才显示正常，JSON 错误或服务错误不能冒充正常。

开发中的请求路径：浏览器页面 → Vite 的 `/health` 转发 → FastAPI → JSON → 页面状态。转发只是本地开发配置，不表示已经部署成可发布的网站，也不需要为此修改后端跨域设置。

后端默认地址为 `http://127.0.0.1:8000`。若更换后端端口，可复制 `frontend/.env.example` 为同目录 `.env` 并修改 `BACKEND_URL`，然后重启 Vite。该配置用于本地开发转发，不放密钥；本次无需复制或修改即可运行。

## 前端工具和检查

React 负责显示随请求变化的页面；TypeScript 在运行前检查类型；Vite 运行和构建网页；Vitest 执行测试，React Testing Library 与 jsdom 模拟页面和点击。没有增加图片服务、UI 组件库或浏览器自动化依赖。

当前已安装依赖。将来在已有 Node.js 的环境恢复安装，可在 frontend 下运行 `npm.cmd ci`，按锁定记录安装；全新电脑安装尚未验证。本次实际环境为 Node.js v24.21.0、npm 11.19.0。

在 frontend 目录运行：

```powershell
npm.cmd run typecheck
npm.cmd run test
npm.cmd run build
```

依次检查数据类型、8 项组件行为、构建文件。组件测试使用模拟请求覆盖等待、成功、失败后恢复、HTTP 错误、错误数据、无效 JSON、超时及请求取消；真实服务连通另行验证，不能由模拟测试替代。构建产物位于 frontend/dist，不提交到仓库。

## 页面验收与学习任务

1. 两端运行时打开 5173 页面，确认显示“后端连接正常”。
2. 仅停止后端，保留前端运行，点击“重新检查”，确认显示“无法连接后端”。
3. 重启后端，再次点击按钮，确认恢复正常。
4. 查看 `frontend/src/api/health.ts` 的 `fetch('/health', ...)`：它就是页面代替你手动访问接口的地方。查看 App.tsx 的三种状态如何选择页面文字。

页面只是显示最近一次检查结果，不是自动持续监控。颜色也不是实际连接的证据，应同时核对状态文字与上述真实操作。用户完成验收前，不标记 Day 1 整体通过。

## 你的验收与理解任务

1. 按说明启动后端，在浏览器看到约定 JSON。
2. 停止后端并刷新，观察无法连接；重新启动后确认恢复。
3. 打开 `backend/app/main.py`，指出哪里规定了访问地址，哪里规定了返回内容。
4. 用自己的话解释：浏览器发送请求，Uvicorn 接收，FastAPI 调用函数并返回 JSON。

完成后把实际结果告诉 Codex。自动测试通过、浏览器实际验证和用户验收是不同状态；前后端运行检查均已获用户明确反馈通过，改动审查及 Day 1 整体确认仍待完成。

## 参考资料

本次测试方式参考 [FastAPI 官方测试教程](https://fastapi.tiangolo.com/tutorial/testing/)。依赖兼容性以本机实际安装与测试结果为准。

本地前端转发参考 [Vite 服务配置](https://vite.dev/config/server-options.html#server-proxy)，组件测试环境参考 [Vitest 测试环境说明](https://vitest.dev/guide/environment.html)。
