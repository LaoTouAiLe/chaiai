// 工具长正文的**行窗口**（v0.8 T73；契约 §19.3）。
//
// 为什么要有这一层：一次工具调用的正文是**有界**的，但"有界"不等于"能看"——一条
// `pnpm run check` 的 stdout 或一次 200 行的读取，展开后仍然会把整段塞进对话流，用户反而
// 找不到重点。窗口只做一件事：**默认露出一屏，其余按需多显示**。
//
// 三条边界（契约写死的，不能在这里各改一套）：
//   1. **只在页面已经拿到的那份内容上翻**：不发请求、不新增工具与参数、不读工作区文件
//      （§1.2 的"不新增读取通道"）；
//   2. **窗口是纯界面状态**：不进快照、不入契约，刷新后回到默认窗口；
//   3. **阈值以下不渲染任何东西**：正文短于一屏时卡片 DOM 与视觉基线一字不变（原型夹具
//      的正文都短于阈值，所以基线不重拍）。
//
// 行号不在这里算：那是契约字段的事（`read` 的 `first_line`、`search` 的 `line_number`）。
// 窗口文案只说"还有多少行没显示"，免得与卡片里的真实行号各说一套。

import { useCallback, useState } from 'react'

/** 一屏显示多少行（§19.3：写死，不开配置开关）。 */
export const LINE_WINDOW = 20

export interface LineWindow {
  /** 这一屏连同之前几屏一共显示了多少行（不会超过 `total`）。 */
  readonly shown: number
  readonly total: number
  /** 还有没显示的行。 */
  readonly more: boolean
  readonly showMore: () => void
  /** 如实标注：`共 n 行`，还有剩余时补上"还有 m 行未显示"。 */
  readonly label: string
}

/** 按窗口切片。纯函数，直接测它。 */
export function windowOf<T>(rows: readonly T[], shown: number): readonly T[] {
  return rows.slice(0, Math.max(0, Math.trunc(shown)))
}

/** 一屏窗口的界面状态。每个卡片各自持有一份，互不影响。 */
export function useLineWindow(total: number): LineWindow {
  const [shown, setShown] = useState(LINE_WINDOW)
  const showMore = useCallback(() => setShown((current) => current + LINE_WINDOW), [])
  // 内容比窗口短（多数情况）时就是全部：不裁、不提示，也不假装还有更多。
  const visible = Math.min(shown, Math.max(0, total))
  const hidden = Math.max(0, total - visible)
  return {
    shown: visible,
    total,
    more: hidden > 0,
    showMore,
    label: hidden > 0 ? `共 ${String(total)} 行 · 还有 ${String(hidden)} 行未显示` : `共 ${String(total)} 行`,
  }
}
