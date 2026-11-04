import { createConverter } from '@deepseek-ai/libreoffice-kit'

const converter = await createConverter({ timeoutMs: 90_000 })
try {
  await converter.render({ inputPath: process.argv[2], outputPath: process.argv[3] })
} finally {
  await converter.dispose()
}
