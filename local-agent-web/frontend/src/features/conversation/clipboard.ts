// 复制到剪贴板（T30）。
//
// 单独一个模块而不是塞进组件文件：组件文件只导出组件，Fast Refresh 才能正常工作
// （`react-refresh/only-export-components` 会因为混着导出函数而报警告）。
//
// 返回**布尔**而不是抛异常：调用方要给用户看"成功/失败"两种结果，而 `navigator.clipboard`
// 在非安全上下文里可能根本不存在——那时必须显示"复制失败"，不能假装成功（用户会粘贴到空内容，
// 还以为是模型没回答完）。

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
