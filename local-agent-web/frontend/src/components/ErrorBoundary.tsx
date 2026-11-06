import { Component, type ErrorInfo, type ReactNode } from 'react'
import styles from './ErrorBoundary.module.css'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
  copied: boolean
}

/**
 * 渲染异常最后一道兜底（2026-11-06）。
 *
 * 没有它的时候，任何一个组件抛错都会让整页变白屏——用户连"哪里出错"都截不下来，只能报"打不开"。
 * 这里把错误信息显示出来并给两件事：重新加载、复制错误信息（发给维护者）。
 *
 * 边界**只接渲染期**的同步错误：事件回调、定时器、Promise 里的异常不会经过这里（那属于全局
 * `window.onerror` 的范畴，见 README 的排障说明）。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, copied: false }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台留完整栈：用户截图时能带上的就是这块。
    console.error('界面渲染失败', error, info.componentStack)
  }

  private copy(): void {
    const { error } = this.state
    if (error === null) return
    const text = `${error.name}: ${error.message}\n${error.stack ?? '(没有 stack)'}`
    void navigator.clipboard?.writeText(text)
      .then(() => { this.setState({ copied: true }) })
      .catch(() => { this.setState({ copied: false }) })
  }

  render(): ReactNode {
    const { error, copied } = this.state
    if (error === null) return this.props.children
    return (
      <div className={styles.root} role="alert">
        <h1 className={styles.title}>页面出错了</h1>
        <p className={styles.lead}>
          这一屏没能画出来。重新加载通常就能继续；如果反复出现，把下面的信息复制给维护者可以定位原因。
        </p>
        <pre className={styles.detail}>{`${error.name}: ${error.message}`}</pre>
        <div className={styles.actions}>
          <button type="button" className={styles.primary} onClick={() => { window.location.reload() }}>
            重新加载
          </button>
          <button type="button" className={styles.secondary} onClick={() => { this.copy() }}>
            {copied ? '已复制' : '复制错误信息'}
          </button>
        </div>
      </div>
    )
  }
}
