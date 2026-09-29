// 漂移检查：重新生成传输类型后必须与已提交的生成文件**逐字节相同**。
//
// 生成物（`src/api/schema.gen.ts`）不得手改：要改契约就改后端 → `uv run python scripts/export_openapi.py`
// → `npm run generate:api`。这个脚本故意不用 `git diff`（生成物可能在未提交的工作树里），
// 而是"先记下当前内容 → 重新生成 → 比对 → 无论成败都还原"，因此**检查不会留下改动**。

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import process from "node:process";

const generated = fileURLToPath(new URL("../src/api/schema.gen.ts", import.meta.url));
const openapi = fileURLToPath(new URL("../../docs/api/openapi.json", import.meta.url));

const before = readFileSync(generated, "utf8");

// 重新生成一次，取回结果后**无论成败都还原**——检查不该在工作树里留下改动。
const regenerated = (() => {
  try {
    // Windows 上 Node 不允许直接 spawn `.cmd`（实测 EINVAL），所以要过一层 shell；
    // 这里的参数全是脚本内的常量路径，没有外部输入拼接。
    execFileSync(
      process.platform === "win32" ? "npx.cmd" : "npx",
      ["--no-install", "openapi-typescript", openapi, "-o", generated],
      { stdio: ["ignore", "ignore", "inherit"], shell: process.platform === "win32" },
    );
    return readFileSync(generated, "utf8");
  } finally {
    writeFileSync(generated, before, "utf8");
  }
})();

if (regenerated !== before) {
  console.error(
    "生成类型与受控 OpenAPI 不一致：请运行 `npm run generate:api` 并提交结果（生成文件不得手改）。",
  );
  process.exit(1);
}
console.log("生成类型与受控 OpenAPI 一致（重新生成无差异）");
