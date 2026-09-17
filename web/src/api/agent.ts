import axios from 'axios'
import { tokenStore } from './http'

const BASE_URL = '/api'

/**
 * 流式聊天：POST /agent/chat 返回 text/plain。
 * 后端 execute_stream 实际是「整段答案一次性 yield」，但仍按流读取以兼容未来逐字输出。
 * onChunk 收到累积文本；返回最终完整文本。
 */
export interface HistoryMsg {
  role: 'user' | 'assistant'
  content: string
}

export async function chatStream(
  query: string,
  onChunk: (accumulated: string) => void,
  signal?: AbortSignal,
  history: HistoryMsg[] = [],
  sessionId = '',
): Promise<string> {
  let token = tokenStore.access

  const doFetch = (tk: string | null) =>
    fetch(`${BASE_URL}/agent/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/plain',
        ...(tk ? { Authorization: `Bearer ${tk}` } : {}),
      },
      // session_id 让后端维护"会话级结构化槽位"（城市/天数/预算），历史被截断也不丢
      body: JSON.stringify({ query, history, session_id: sessionId || null }),
      signal,
    })

  let resp = await doFetch(token)

  // token 过期：刷新一次再重试
  if ((resp.status === 401 || resp.status === 403) && tokenStore.refresh) {
    try {
      const r = await axios.post(
        `${BASE_URL}/user/refresh-token`,
        { refresh_token: tokenStore.refresh },
        { timeout: 15000 },
      )
      const newAccess = r.data?.access_token as string
      if (newAccess) {
        tokenStore.saveAccess(newAccess)
        token = newAccess
        resp = await doFetch(token)
      }
    } catch {
      /* 刷新失败，下面按错误处理 */
    }
  }

  if (!resp.ok || !resp.body) {
    let detail = '连接不上服务器，请检查网络后再试'
    try {
      const data = await resp.json()
      if (typeof data?.detail === 'string') detail = data.detail
    } catch {
      /* 忽略解析错误 */
    }
    throw new Error(detail)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    onChunk(buffer)
  }
  return buffer
}
