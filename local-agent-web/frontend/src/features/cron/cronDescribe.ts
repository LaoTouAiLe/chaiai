// cron 表达式 → 中文（2026-10-23）。
//
// 只解释**标准 5 段**（分 时 日 月 周）。三条边界，页面上必须如实体现：
// 1. 这不是权威校验：后端用 croniter，能接受一些这里解释不了的扩展写法（L / W / # / 秒级 6 段）。
//    所以解析失败**不阻止提交**，只提示"看不懂"，最终仍由后端 422 兜底。
// 2. 不做"下次触发时间"：那需要 croniter 或等价实现，前端算出来的时间一旦和后端差一分钟，
//    比不显示更糟。
// 3. 日（DOM）与周（DOW）同时指定时，cron 的语义是"或"而不是"且"——这种情况必须写出来。

type Part =
  | { kind: 'any' }
  | { kind: 'steps'; steps: readonly number[]; values: readonly number[] }

export type CronDescription =
  | { ok: true; text: string }
  | { ok: false; reason: string }

const DOW_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] as const

interface FieldSpec {
  label: string
  min: number
  max: number
}

const FIELDS: readonly FieldSpec[] = [
  { label: '分钟', min: 0, max: 59 },
  { label: '小时', min: 0, max: 23 },
  { label: '日', min: 1, max: 31 },
  { label: '月', min: 1, max: 12 },
  { label: '周', min: 0, max: 7 },
]

const pad = (value: number): string => String(value).padStart(2, '0')

function parseNumber(raw: string, spec: FieldSpec): number {
  if (!/^\d+$/.test(raw)) throw new Error(`${spec.label}段「${raw}」不是数字`)
  const value = Number(raw)
  if (value < spec.min || value > spec.max) {
    throw new Error(`${spec.label}只能是 ${spec.min}–${spec.max}，收到「${raw}」`)
  }
  return value
}

/** 一段（如 `1-5`、`* 配 /30`、`0,30`）。支持扩展写法的段会在这里如实失败。 */
function parsePart(raw: string, spec: FieldSpec): Part {
  const text = raw.trim()
  if (text === '') throw new Error(`${spec.label}段是空的`)
  if (text === '*') return { kind: 'any' }
  if (/[LW#?]/i.test(text)) {
    throw new Error(`${spec.label}段「${text}」用了扩展写法（L / W / # / ?），页面暂不解释`)
  }

  const steps: number[] = []
  const values: number[] = []
  for (const item of text.split(',')) {
    if (item.trim() === '') throw new Error(`${spec.label}段里有空的项`)

    const [range, stepText, extra] = item.split('/')
    if (extra !== undefined) throw new Error(`${spec.label}段「${item}」格式不对`)

    if (stepText !== undefined) {
      const step = parseNumber(stepText, { ...spec, label: `${spec.label}的步长` })
      if (step < 1) throw new Error(`${spec.label}的步长必须 ≥ 1`)
      // 步长只保留语义（每 N），不展开成具体值：展开会丢掉"每 30 分钟"这层意思。
      if (range === '*') {
        steps.push(step)
        continue
      }
      const [fromText, toText] = range.split('-')
      if (toText === undefined) throw new Error(`${spec.label}段「${item}」需要写成 起始-结束/步长`)
      parseNumber(fromText, spec)
      parseNumber(toText, spec)
      steps.push(step)
      continue
    }

    const [fromText, toText] = range.split('-')
    if (toText === undefined) {
      values.push(parseNumber(fromText, spec))
      continue
    }
    const from = parseNumber(fromText, spec)
    const to = parseNumber(toText, spec)
    if (from > to) throw new Error(`${spec.label}段「${item}」的起点大于终点`)
    for (let value = from; value <= to; value += 1) values.push(value)
  }

  if (steps.length > 0 && values.length > 0) {
    throw new Error(`${spec.label}段把"每 N"和具体值混在一起，页面暂不解释`)
  }
  if (steps.length > 0) return { kind: 'steps', steps, values: [] }
  return { kind: 'steps', steps: [], values: [...new Set(values)].sort((a, b) => a - b) }
}

/** 把值列表压成区间文字：1,2,3,4,5 → "1 日至 5 日"。 */
function joinValues(values: readonly number[], suffix: string): string {
  if (values.length === 0) return ''
  const contiguous = values.every((value, index) => index === 0 || value === values[index - 1] + 1)
  if (contiguous && values.length > 2) return `${values[0]}${suffix}至 ${values[values.length - 1]}${suffix}`
  return values.map(value => `${value}${suffix}`).join('、')
}

function describeDayOfWeek(part: Part): string | null {
  if (part.kind === 'any') return null
  if (part.steps.length > 0) return `每 ${part.steps[0]} 天（按周计）`

  const days = [...new Set(part.values.map(value => (value === 7 ? 0 : value)))].sort((a, b) => a - b)
  if (days.length === 0) return null
  if (days.length === 5 && days.every((day, index) => day === index + 1)) return '工作日'
  if (days.length === 2 && days[0] === 0 && days[1] === 6) return '周末'

  const contiguous = days.every((day, index) => index === 0 || day === days[index - 1] + 1)
  if (contiguous && days.length > 2) return `${DOW_NAMES[days[0]]}至${DOW_NAMES[days[days.length - 1]]}`
  return days.map(day => DOW_NAMES[day]).join('、')
}

function describeDayOfMonth(part: Part): string | null {
  if (part.kind === 'any') return null
  if (part.steps.length > 0) return `每 ${part.steps[0]} 天`
  return joinValues(part.values, ' 日')
}

function describeMonth(part: Part): string | null {
  if (part.kind === 'any') return null
  if (part.steps.length > 0) return `每 ${part.steps[0]} 个月`
  return joinValues(part.values, ' 月')
}

function formatHours(part: Part): string {
  if (part.kind === 'any') return '任意小时'
  const hours = part.values
  const contiguous = hours.every((value, index) => index === 0 || value === hours[index - 1] + 1)
  if (contiguous && hours.length > 2) return `${hours[0]} 点至 ${hours[hours.length - 1]} 点`
  return hours.map(value => `${value} 点`).join('、')
}

/**
 * 时间部分：能拼出具体时刻（09:00、18:00）就拼，拼不出才退回节奏描述。
 *
 * 每个分支都要覆盖"分钟任意 / 分钟有值 / 分钟是步长 × 小时任意 / 有值 / 是步长"这几种组合，
 * 否则会把 `* * * * *` 说成"每小时的第 0 分"——那是最容易写错、也最容易骗过自己的一种错。
 */
function describeTime(minute: Part, hour: Part): string {
  const minuteAny = minute.kind === 'any'
  const hourAny = hour.kind === 'any'

  if (minuteAny && hourAny) return '每分钟'

  if (minute.kind === 'steps' && minute.steps.length > 0) {
    const step = minute.steps[0]
    const cadence = step === 1 ? '每分钟' : `每 ${step} 分钟`
    return hourAny ? cadence : `${cadence}（仅在 ${formatHours(hour)}）`
  }

  if (hour.kind === 'steps' && hour.steps.length > 0) {
    const step = hour.steps[0]
    if (minuteAny) return `每 ${step} 小时`
    return `每 ${step} 小时的第 ${minute.values.join('、')} 分`
  }

  const minutes = minuteAny ? null : minute.values
  const hours = hourAny ? null : hour.values

  if (hours === null) {
    if (minutes === null) return '每分钟'
    return minutes.length === 1 ? `每小时的第 ${minutes[0]} 分` : `每小时的第 ${minutes.join('、')} 分`
  }
  if (minutes === null) return `${formatHours(hour)} 的每一分钟`

  const moments = hours.flatMap(h => minutes.map(m => `${pad(h)}:${pad(m)}`))
  if (moments.length <= 4) return moments.join('、')
  return `${moments.slice(0, 4).join('、')} 等 ${moments.length} 个时刻`
}

function describeDate(dom: Part, month: Part, dow: Part): string {
  const dayOfWeek = describeDayOfWeek(dow)
  const dayOfMonth = describeDayOfMonth(dom)
  const monthText = describeMonth(month)
  // "工作日""周末"已经是完整短语，不能再加"每"（"每工作日"读不通）。
  const dowPhrase = dayOfWeek === null
    ? null
    : (dayOfWeek === '工作日' || dayOfWeek === '周末' ? dayOfWeek : `每${dayOfWeek}`)

  if (dowPhrase === null && dayOfMonth === null) {
    if (monthText === null) return '每天'
    return `每年 ${monthText}`
  }
  if (dowPhrase !== null && dayOfMonth === null) {
    return monthText === null ? dowPhrase : `每年 ${monthText}的${dowPhrase}`
  }
  if (dayOfMonth !== null && dowPhrase === null) {
    return monthText === null ? `每月 ${dayOfMonth}` : `每年 ${monthText} ${dayOfMonth}`
  }
  // 两者都写：cron 里是"或"，不是"且"。写错这一条会让人以为要同时满足。
  return `每月 ${dayOfMonth} 或${dowPhrase}（cron 的"或"语义）`
}

/**
 * 解析 5 段 cron 并给出中文说明。
 *
 * 解析失败时 `reason` 是给用户看的短句；**调用方不应据此禁止提交**——
 * 权威校验在后端（`next_run_at` 抛 ValueError → 422）。
 */
export function describeCron(expression: string): CronDescription {
  const text = (expression ?? '').trim()
  if (text === '') return { ok: false, reason: '还没填表达式' }

  const parts = text.split(/\s+/)
  if (parts.length !== 5) {
    return { ok: false, reason: `需要 5 段（分 时 日 月 周），现在是 ${parts.length} 段` }
  }

  try {
    const [minute, hour, dom, month, dow] = parts.map((part, index) => parsePart(part, FIELDS[index]))
    const date = describeDate(dom, month, dow)
    const time = describeTime(minute, hour)
    // 时刻型（"09:00"）补上"每天"；节奏型（"每 30 分钟"）再加就成了"每天每 30 分钟"。
    if (date === '每天') return { ok: true, text: time.startsWith('每') ? time : `每天 ${time}` }
    return { ok: true, text: `${date} ${time}` }
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : '无法解析这个表达式' }
  }
}
