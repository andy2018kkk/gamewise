import { useEffect, useState } from 'react'
import { checkHealth } from './api/health'

type ConnectionState = 'checking' | 'connected' | 'disconnected'

const messages: Record<ConnectionState, { title: string; description: string }> = {
  checking: { title: '正在检查后端连接', description: '正在等待本机服务响应，请稍候。' },
  connected: { title: '后端连接正常', description: '已收到有效的健康响应。你可以继续下一步开发。' },
  disconnected: { title: '无法连接后端', description: '未收到有效的健康响应。请确认后端已启动，再重新检查。' },
}

export default function App() {
  const [connection, setConnection] = useState<ConnectionState>('checking')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = window.setTimeout(() => controller.abort(), 5000)

    async function runCheck() {
      try {
        await checkHealth(controller.signal)
        if (active) setConnection('connected')
      } catch {
        if (active) setConnection('disconnected')
      } finally {
        window.clearTimeout(timeout)
      }
    }

    void runCheck()
    return () => {
      active = false
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [attempt])

  function retry() {
    setConnection('checking')
    setAttempt((previous) => previous + 1)
  }

  return (
    <main className="workspace">
      <header className="masthead">
        <a className="brand" href="/" aria-label="GameWise AI 首页">
          <span className="brand-mark" aria-hidden="true">G</span>
          <span>GameWise<span className="brand-ai"> AI</span></span>
        </a>
        <span className="local-label"><span aria-hidden="true" />本地开发</span>
      </header>

      <section className="intro" aria-labelledby="page-title">
        <p className="eyebrow">先连接，再探索</p>
        <h1 id="page-title">GameWise AI</h1>
        <p className="intro-copy">你的游戏攻略助手，从可靠的连接开始。</p>
      </section>

      <section className="connection-panel" aria-labelledby="connection-title">
        <div className="panel-heading">
          <h2 id="connection-title">后端连接</h2>
          <span className="endpoint">/health</span>
        </div>
        <div className={`status-block ${connection}`} role="status" aria-live="polite">
          <span className="status-indicator" aria-hidden="true" />
          <div>
            <p className="status-title">{messages[connection].title}</p>
            <p className="status-description">{messages[connection].description}</p>
          </div>
        </div>
        <button type="button" onClick={retry} disabled={connection === 'checking'}>
          {connection === 'checking' ? '检查中…' : '重新检查'}
          <span aria-hidden="true"> ↗</span>
        </button>
        <p className="check-note">打开页面时检查一次；服务状态变化后，请点击重新检查。</p>
      </section>

      <footer>
        <span className="footer-label">当前进度</span>
        <p>正在建立项目基础。游戏资料检索与 AI 问答尚未接入。</p>
      </footer>
    </main>
  )
}
