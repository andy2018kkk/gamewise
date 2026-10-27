export async function checkHealth(signal: AbortSignal): Promise<void> {
  const response = await fetch('/health', { signal, cache: 'no-store' })
  if (!response.ok) throw new Error('健康接口返回错误状态')

  const data: unknown = await response.json()
  if (
    typeof data !== 'object' || data === null ||
    !('status' in data) || data.status !== 'ok'
  ) {
    throw new Error('健康接口未返回约定内容')
  }
}
