import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import QuestionPanel from './QuestionPanel'

describe('网页问答', () => {
  it('把Markdown显示为标题、加粗、列表和表格，保留引用，不执行HTML', async () => {
    const answer = '## 推荐结论\n\n**优先考虑甲**，理由见[1]。\n\n- 有资源能力\n- 存在技能代价\n\n| 候选 | 优势 |\n| --- | --- |\n| 甲 | 摸牌 |\n| 乙 | 自保 |\n\n<script>window.unwanted = true</script>\n\n<img src="bad" onerror="alert(1)">'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: 'preview_answer', answer, sources: [], steps: [], error: null, elapsed_seconds: 1, preview: true,
    }))))
    const { container } = render(<QuestionPanel />)
    fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: '比较两个候选' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    await screen.findByRole('heading', { name: '推荐结论', level: 2 })
    expect(screen.getByText('优先考虑甲').tagName).toBe('STRONG')
    expect(screen.getByText('有资源能力').tagName).toBe('LI')
    expect(screen.getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['候选', '优势'])
    expect(screen.getByRole('table').parentElement?.className).toBe('answer-table-scroll')
    expect(container.querySelector('.answer-markdown')?.textContent).toContain('[1]')
    expect(container.querySelector('script, img')).toBeNull()
    expect(container.querySelector('.answer-markdown')?.textContent).not.toContain('window.unwanted')
  })

  it('只发送用户问题，等待时禁止重复发送，展示答案来源与过程', async () => {
    let finish!: (response: Response) => void
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve }))
    vi.stubGlobal('fetch', fetchMock)
    render(<QuestionPanel />)
    const button = screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: '移动版孙坚的技能' } })
    fireEvent.click(button)
    expect(button.disabled).toBe(true)
    expect((screen.getByRole('button', { name: '新对话' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(button)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]).toEqual(['/api/ask', expect.objectContaining({
      body: JSON.stringify({ question: '移动版孙坚的技能', history: [] }), method: 'POST',
    })])
    finish(new Response(JSON.stringify({ status: 'preview_answer', answer: '技能说明[1]',
      sources: [{ number: 1, title: '孙坚 · 英魂', game_id: 'sanguosha-mobile', text: '技能原文', url: 'https://example.com/source' }],
      steps: [{ action: '查询武将技能', records: 1, error: null }], error: null, elapsed_seconds: 2, preview: true })))
    await screen.findByText('技能说明[1]')
    const sources = screen.getByText(/来源与原文/)
    expect(sources.parentElement?.hasAttribute('open')).toBe(false)
    fireEvent.click(sources)
    fireEvent.click(screen.getByText('孙坚 · 英魂'))
    expect(screen.getByText('技能原文')).toBeTruthy()
    expect(screen.getByRole('link', { name: '查看原网页 ↗' }).getAttribute('href')).toBe('https://example.com/source')
    expect(screen.getByText('查询武将技能：返回 1 条资料')).toBeTruthy()
    expect((screen.getByLabelText('你的问题') as HTMLTextAreaElement).disabled).toBe(false)
    expect(screen.getByText('移动版孙坚的技能', { selector: 'p' })).toBeTruthy()
  })

  it('显示模型故障，不自动重跑', async () => {
    const mock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'error', answer: null,
      sources: [], steps: [], error: 'Groq HTTP 429', elapsed_seconds: 1, preview: true })))
    vi.stubGlobal('fetch', mock)
    render(<QuestionPanel />)
    fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: '刘备' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    expect((await screen.findByRole('alert')).textContent).toContain('429')
    expect(mock).toHaveBeenCalledTimes(1)
  })

  it('网络失败后恢复发送，不展示伪答案', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('网络断开')))
    render(<QuestionPanel />)
    fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: '曹操' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    await screen.findByRole('alert')
    await waitFor(() => expect((screen.getByRole('button', { name: '发送问题' }) as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByText('回答')).toBeNull()
  })

  it('异常响应不让页面崩溃或当作成功答案', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: 'preview_answer', sources: [null], steps: [],
    }))))
    render(<QuestionPanel />)
    fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: '刘备' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    expect((await screen.findByRole('alert')).textContent).toContain('内容不完整')
  })

  it('示例仅填入并聚焦输入框，不调用模型', () => {
    const mock = vi.fn()
    vi.stubGlobal('fetch', mock)
    render(<QuestionPanel />)
    fireEvent.click(screen.getByRole('button', { name: /懂规则/ }))
    const input = screen.getByLabelText('你的问题') as HTMLTextAreaElement
    expect(input.value).toBe('失去体力和受到伤害有什么区别？')
    expect(document.activeElement).toBe(input)
    expect(mock).not.toHaveBeenCalled()
  })

  it('追问带上上一轮文字并保留消息，清空对话后不再携带', async () => {
    const mock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({
      status: 'preview_answer', answer: '已答复', sources: [], steps: [], error: null, elapsed_seconds: 1, preview: true,
    }))))
    vi.stubGlobal('fetch', mock)
    render(<QuestionPanel />)
    const input = screen.getByLabelText('你的问题')
    fireEvent.change(input, { target: { value: '第一题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    await screen.findByText('已答复')
    fireEvent.change(input, { target: { value: '第二题' } })
    expect(screen.getByText('第一题')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    await waitFor(() => expect(screen.getAllByText('已答复')).toHaveLength(2))
    expect(screen.getByText('第一题', { selector: 'p' })).toBeTruthy()
    expect(screen.getByText('第二题', { selector: 'p' })).toBeTruthy()
    expect(JSON.parse(mock.mock.calls[1][1].body)).toEqual({ question: '第二题', history: [
      { question: '第一题', answer: '已答复' },
    ] })
    fireEvent.click(screen.getByRole('button', { name: '新对话' }))
    expect(screen.queryByText('第一题', { selector: 'p' })).toBeNull()
    expect(screen.getByRole('heading', { name: '今天想了解什么？' })).toBeTruthy()
    fireEvent.change(input, { target: { value: '第三题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    await screen.findByText('已答复')
    expect(JSON.parse(mock.mock.calls[2][1].body)).toEqual({ question: '第三题', history: [] })
  })

  it('只发送最近两轮成功问答，失败轮不加入历史，刷新后清空', async () => {
    let count = 0
    const mock = vi.fn().mockImplementation(() => {
      count += 1
      return Promise.resolve(new Response(JSON.stringify({ status: count === 2 ? 'error' : 'preview_answer',
        answer: count === 2 ? null : `答复${count}`, sources: [], steps: [],
        error: count === 2 ? '平台限流' : null, elapsed_seconds: 1, preview: true })))
    })
    vi.stubGlobal('fetch', mock)
    const mounted = render(<QuestionPanel />)
    for (let index = 1; index <= 5; index += 1) {
      fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: `问题${index}` } })
      fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
      if (index === 2) await screen.findByRole('alert')
      else await screen.findByText(`答复${index}`)
    }
    expect(JSON.parse(mock.mock.calls[2][1].body).history).toEqual([{ question: '问题1', answer: '答复1' }])
    expect(JSON.parse(mock.mock.calls[4][1].body).history).toEqual([
      { question: '问题3', answer: '答复3' }, { question: '问题4', answer: '答复4' },
    ])
    expect(screen.getByText('问题1', { selector: 'p' })).toBeTruthy()
    mounted.unmount()
    render(<QuestionPanel />)
    expect(screen.queryByText('问题1', { selector: 'p' })).toBeNull()
    fireEvent.change(screen.getByLabelText('你的问题'), { target: { value: '刷新后的问题' } })
    fireEvent.click(screen.getByRole('button', { name: '发送问题' }))
    await screen.findByText('答复6')
    expect(JSON.parse(mock.mock.calls[5][1].body).history).toEqual([])
  })
})
