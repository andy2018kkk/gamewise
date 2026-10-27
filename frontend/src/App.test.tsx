import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from './App'

function healthyResponse() {
  return new Response(JSON.stringify({ status: 'ok' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('后端连接状态', () => {
  it('检查期间显示等待状态并禁止重复点击', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    render(<App />)

    expect(screen.getByRole('status').textContent).toContain('正在检查')
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('取得有效健康响应后显示连接正常', async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthyResponse())
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)

    await screen.findByText('后端连接正常')
    expect(fetchMock.mock.calls[0][0]).toBe('/health')
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false)
  })

  it('网络失败后允许重新检查，并可恢复正常', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(healthyResponse())
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)

    await screen.findByText('无法连接后端')
    fireEvent.click(screen.getByRole('button', { name: '重新检查' }))
    await screen.findByText('后端连接正常')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['HTTP 错误', () => new Response('Unavailable', { status: 503 })],
    ['异常 JSON 内容', () => new Response(JSON.stringify({ status: 'error' }))],
    ['无效 JSON', () => new Response('<html>错误页面</html>')],
  ])('%s 不得显示为连接正常', async (_name, response) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response()))
    render(<App />)

    await screen.findByText('无法连接后端')
    expect(screen.queryByText('后端连接正常')).toBeNull()
  })

  it('请求超时后退出等待并允许重试', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((_url: string, options: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => reject(new Error('aborted')))
      }),
    ))
    render(<App />)

    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.getByRole('status').textContent).toContain('无法连接后端')
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false)
  })

  it('页面卸载时取消尚未完成的请求', async () => {
    const fetchMock = vi.fn((_url: string, _options: RequestInit) => new Promise(() => {}))
    vi.stubGlobal('fetch', fetchMock)
    const { unmount } = render(<App />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined

    unmount()
    expect(options?.signal?.aborted).toBe(true)
  })
})
