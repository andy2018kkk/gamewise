import { useEffect, useState } from 'react'
import { checkHealth } from './api/health'
import QuestionPanel from './components/QuestionPanel'

type ConnectionState = 'checking' | 'connected' | 'disconnected'

const messages: Record<ConnectionState, { title: string; description: string }> = {
  checking: { title: '正在检查后端连接', description: '正在等待本机服务响应，请稍候。' },
  connected: { title: '后端连接正常', description: '已收到有效的健康响应，可以在下方输入问题。' },
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
    <div className="workspace">
      <header className="masthead">
        <a className="brand" href="/" aria-label="GameWise AI 首页">
          <span className="brand-mark" aria-hidden="true">G</span>
          <span>GameWise<span className="brand-ai"> AI</span></span>
        </a>
        <div className="header-tools">
          <span className="local-label">本地资料预览</span>
          <details className={`connection-menu ${connection}`}>
            <summary aria-label="服务连接详情">
              <span className="status-indicator" aria-hidden="true" />
              <span role="status" aria-live="polite">{messages[connection].title}</span>
              <span className="chevron" aria-hidden="true">⌄</span>
            </summary>
            <div className="connection-popover">
              <h2>服务连接</h2>
              <p>{messages[connection].description}</p>
              <button type="button" onClick={retry} disabled={connection === 'checking'}>
                {connection === 'checking' ? '检查中…' : '重新检查'}
              </button>
              <p className="check-note">连接检查不调用模型；状态变化后可手动重查。</p>
            </div>
          </details>
        </div>
      </header>
      <main className="chat-main"><QuestionPanel /></main>
    </div>
  )
}
