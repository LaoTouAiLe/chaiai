import { DeepSeekHarness } from 'file:///D:/ideaTest2/deepseek-harness/packages/sdk/client/lib/index.js'
import { readFile, writeFile } from 'node:fs/promises'
import { resolve, join } from 'node:path'

const out = resolve(process.argv[2])
const scenario = JSON.parse(await readFile(join(out, 'baseline/scenario.json'), 'utf8'))
const harness = new DeepSeekHarness({
  launch: {
    command: 'node',
    args: ['D:/ideaTest2/deepseek-harness/apps/cli/lib/bin.js', '--profile', 'headless',
      '--patch', join(out, 'dsh-sdk-patch.yaml')],
    cwd: scenario.workspace,
    env: { ...process.env, DSH_HOME: join(out, 'dsh-sdk-home'), DSH_TELEMETRY_DISABLED: '1' },
    requestTimeoutMs: 1_800_000,
  },
  cwd: scenario.workspace,
  provider: 'deepseek-official', model: 'deepseek-flash', maxTokens: 4096,
})
try {
  let sessionId
  for (let i = 0; i < scenario.turns.length; i++) {
    const startedAt = Date.now() / 1000
    console.log(`dsh turn ${i + 1} started`)
    const result = await harness.run(scenario.turns[i], { sessionId })
    sessionId = result.sessionId
    await writeFile(join(out, `dsh-sdk-turn-${i + 1}.json`), JSON.stringify({
      startedAt, finishedAt: Date.now() / 1000, prompt: scenario.turns[i], ...result,
    }, null, 2))
    await writeFile(join(out, `dsh-sdk-answer-${i + 1}.md`), result.finalResponse)
    console.log(`dsh turn ${i + 1} finished; ${result.events.length} durable events`)
  }
} finally {
  await harness.close()
}
