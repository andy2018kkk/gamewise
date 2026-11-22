import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { askQuestion } from '../api/ask'
import type { AskAnswer } from '../api/ask'

const suggestions = [
  { category: '查技能', question: '三国杀 OL 孙坚有哪些系列和技能？' },
  { category: '懂规则', question: '失去体力和受到伤害有什么区别？' },
  { category: '做比较', question: '三服的裴秀各有什么优势？' },
]

const gameNames: Record<string, string> = {
  'sanguosha-ol': '三国杀 OL', 'sanguosha-shizhounian': '三国杀十周年', 'sanguosha-mobile': '三国杀移动版',
}

interface ChatTurn {
  question: string
  result: AskAnswer | null
  error: string
}

export default function QuestionPanel() {
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState<ChatTurn[]>([])
  const [pending, setPending] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || !question.trim()) return
    const current = question.trim()
    const index = messages.length
    const history = messages.flatMap((turn) => turn.result?.answer && !turn.result.error && !turn.error
      ? [{ question: turn.question, answer: turn.result.answer }] : []).slice(-2)
    setMessages((previous) => [...previous, { question: current, result: null, error: '' }])
    setQuestion('')
    setPending(true)
    try {
      const result = await askQuestion(current, history)
      setMessages((previous) => previous.map((turn, position) => position === index ? { ...turn, result } : turn))
      if (result.error) setQuestion(current)
    } catch (failure) {
      const error = failure instanceof Error ? failure.message : '无法连接问答服务，请检查后端。'
      setMessages((previous) => previous.map((turn, position) => position === index ? { ...turn, error } : turn))
      setQuestion(current)
    } finally {
      setPending(false)
    }
  }

  function chooseSuggestion(value: string) {
    setQuestion(value)
    input.current?.focus()
  }

  function newConversation() {
    if (pending) return
    setMessages([])
    setQuestion('')
    input.current?.focus()
  }

  return <section className={`question-panel ${messages.length ? 'has-question' : 'is-empty'}`} aria-label="游戏问答">
    {!messages.length && <div className="welcome">
      <div className="welcome-symbol" aria-hidden="true">✦</div>
      <p className="eyebrow">GAMEWISE AI · 游戏资料助手</p>
      <h1>今天想了解什么？</h1>
      <p className="welcome-copy">从一个技能，到一次选择。带着资料，聊聊你的游戏问题。</p>
      <div className="game-badges"><span>三国杀 OL</span><span>十周年</span><span>移动版</span></div>
      <div className="suggestions" aria-label="示例问题">
        {suggestions.map((item) => <button type="button" className="suggestion" key={item.category}
          onClick={() => chooseSuggestion(item.question)}>
          <span className="suggestion-category">{item.category}<span aria-hidden="true">↗</span></span>
          <span>{item.question}</span>
        </button>)}
      </div>
    </div>}

    {messages.length > 0 && <div className="conversation" aria-label="当前对话">
      <div className="conversation-toolbar"><span>当前对话</span>
        <button type="button" onClick={newConversation} disabled={pending}>新对话</button>
      </div>
      {messages.map(({ question: sentQuestion, result, error }, index) => <div className="conversation-turn" key={index}>
      <div className="user-message"><span className="message-label">你</span><p>{sentQuestion}</p></div>
      <article className="assistant-message" aria-label="助手回答">
        <div className="assistant-heading"><span className="assistant-mark" aria-hidden="true">G</span><span>GameWise</span>
          {result && <span className="response-time">{result.elapsed_seconds.toFixed(1)} 秒</span>}
        </div>
        {pending && index === messages.length - 1 && <div className="thinking" role="status"><span className="thinking-dot" aria-hidden="true" />
          <div>正在查阅资料，整理回答…<p>遇到平台限流可能需要等待。</p></div>
        </div>}
        {(error || result?.error) && <p role="alert" className="ask-error">{error || result?.error}</p>}
        {result && <div className="ask-result">
          {result.answer && <div className="answer-markdown">
            <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
              table: ({ children }) => <div className="answer-table-scroll"><table>{children}</table></div>,
            }}>{result.answer}</Markdown>
          </div>}
          {result.answer && result.sources.length === 0 && <p className="check-note">本次回答未提供可关联引用，请核对后再采信。</p>}
          {result.sources.length > 0 && <details className="sources-panel">
            <summary>来源与原文 <span className="detail-count">{result.sources.length}</span></summary>
            <ol className="sources">{result.sources.map((source) => <li key={source.number} value={source.number}>
              <details><summary>{source.title}<span className="source-game">{gameNames[source.game_id] ?? source.game_id}</span></summary>
                <p className="answer-text">{source.text}</p>
                {source.url && /^https?:\/\//i.test(source.url) && <a href={source.url} target="_blank" rel="noreferrer">查看原网页 ↗</a>}
              </details>
            </li>)}</ol>
          </details>}
          <details className="query-steps"><summary>本次查询过程 <span className="detail-count">{result.steps.length} 步</span></summary>
            <ol>{result.steps.map((step, index) => <li key={index}>{step.action}：返回 {step.records} 条资料{step.error && `；${step.error}`}</li>)}</ol>
          </details>
          {result.answer && <p className="check-note">分析基于本次资料，重要结论可展开来源核对。</p>}
        </div>}
      </article>
      </div>)}
    </div>}

    <div className="composer-area">
      <form className="composer" onSubmit={(event) => { void submit(event) }}>
        <label className="sr-only" htmlFor="question">你的问题</label>
        <textarea ref={input} id="question" value={question} onChange={(event) => setQuestion(event.target.value)}
          disabled={pending} placeholder="问问武将、规则，或比较不同版本…" rows={3} />
        <div className="composer-toolbar"><span>基于本地资料 · Qwen</span>
          <button className="send-button" type="submit" disabled={pending || !question.trim()}>
            {pending ? '查询中' : '发送问题'}<span aria-hidden="true">↑</span>
          </button>
        </div>
      </form>
      <p className="composer-note">支持简短追问，每次仅带最近两轮成功问答；更早记录不传入模型，刷新或新对话会清空。资料覆盖有限。</p>
    </div>
  </section>
}
