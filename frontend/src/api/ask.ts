export interface AskAnswer {
  status: string
  answer: string | null
  sources: { number: number; title: string; game_id: string; text: string; url: string | null }[]
  steps: { action: string; records: number; error: string | null }[]
  error: string | null
  elapsed_seconds: number
  preview: boolean
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isAnswer(value: unknown): value is AskAnswer {
  return isObject(value) && typeof value.status === 'string' &&
    (value.answer === null || typeof value.answer === 'string') &&
    (value.error === null || typeof value.error === 'string') &&
    typeof value.elapsed_seconds === 'number' && Number.isFinite(value.elapsed_seconds) &&
    typeof value.preview === 'boolean' &&
    Array.isArray(value.sources) && value.sources.every((source: unknown) => isObject(source) &&
      typeof source.number === 'number' && typeof source.title === 'string' &&
      typeof source.game_id === 'string' && typeof source.text === 'string' &&
      (source.url === null || typeof source.url === 'string')) &&
    Array.isArray(value.steps) && value.steps.every((step: unknown) => isObject(step) &&
      typeof step.action === 'string' && typeof step.records === 'number' &&
      (step.error === null || typeof step.error === 'string'))
}

export interface ConversationTurn {
  question: string
  answer: string
}

export async function askQuestion(question: string, history: ConversationTurn[] = []): Promise<AskAnswer> {
  const response = await fetch('/api/ask', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, history }),
  })
  if (!response.ok) throw new Error(response.status === 422 ? '请输入有效问题。' : '问答服务暂时无法响应，请检查后端。')
  const data: unknown = await response.json()
  if (!isAnswer(data)) throw new Error('后端返回的问答内容不完整。')
  return data
}
