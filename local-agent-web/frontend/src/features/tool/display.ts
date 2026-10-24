import type { ToolRowView } from '../../fixtures/types'

export const TOOL_STATUS_LABEL: Record<ToolRowView['status'], string> = {
  pending: '待执行',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  uncertain: '结果未确认',
}

/** Short labels for actions. Raw tool identifiers stay in the event contract. */
const LABELS: Record<string, string> = {
  list_dir: '查看目录',
  read_file: '读取文件',
  edit_file: '修改文件',
  write_file: '写入文件',
  delete_file: '删除文件',
  run_shell: '运行命令',
  execute_code: '运行代码',
  start_shell: '启动交互进程',
  read_shell: '读取进程输出',
  write_shell: '发送终端输入',
  stop_shell: '停止交互进程',
  list_shell_sessions: '查看后台进程',
  browser: '操作浏览器',
  download_file: '下载文件',
  inspect_system: '查询本机状态',
  grep: '搜索内容',
  glob: '查找文件',
  search_files: '搜索文件',
  todo_write: '更新任务清单',
  update_plan: '更新计划',
  // §18.7：自主委派。不写这一条，工具行会退化成通用兜底文案"执行操作"。
  delegate_research: '委派只读调查',
}

export function toolLabel(name: string): string {
  return LABELS[name] ?? (Object.values(LABELS).includes(name) ? name : '执行操作')
}

const PARAMETER_LABELS: Record<string, string> = {
  path: '路径', cwd: '工作目录', command: '命令', timeout_seconds: '超时秒数',
  recursive: '包含子目录', overwrite: '覆盖已有文件', old_text: '原内容预览',
  new_text: '新内容预览', patch: '修改预览', content: '内容预览', replace_all: '替换所有匹配',
  reason: '操作原因', start_line: '开始行', end_line: '结束行', pattern: '查找内容',
  glob: '文件范围', output_mode: '结果形式', ignore_case: '忽略大小写',
  include_hidden: '包含隐藏文件', max_entries: '条目上限', max_chars: '字符上限',
  max_matches: '匹配上限', max_files: '文件上限', env_keys: '环境变量名', explanation: '计划说明',
  tasks: '子任务', concurrency: '同时执行数', scope: '目录', question: '问题',
  session_id: '进程会话', action: '操作', url: '网址', selector: '页面控件',
  text: '输入内容', eof: '关闭输入', wait_seconds: '等输出秒数', tab_id: '浏览器标签',
  visible: '显示窗口', width: '宽度', height: '高度', key: '按键', offset: '偏移', pid: '进程编号',
  control_offset: '控件偏移',
  delta_y: '垂直滚动', delta_x: '水平滚动',
}
export function parameterLabel(name: string): string { return PARAMETER_LABELS[name] ?? '参数' }
