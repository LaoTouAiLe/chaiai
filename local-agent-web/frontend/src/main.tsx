import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app/App'
import './styles/tokens.css'
import './styles/base.css'

// 服务端状态的重试口径：
// - 查询：网络抖动重试一次；本机单用户场景下"切回窗口就重拉"只会让流式尾部闪，关掉。
// - 变更（提交消息、审批）：**绝不自动重试**。提交的幂等键重试必须由用户发起（见 `api/queries.ts`），
//   自动重试会把"没成功"伪装成"在重发"，用户就没机会看清到底发生了什么。
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
    mutations: { retry: false },
  },
})

const container = document.getElementById('root')
if (container === null) {
  throw new Error('缺少 #root 容器')
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
