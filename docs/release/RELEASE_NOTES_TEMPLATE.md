# Release notes 模板

> 用途：`release.yml` 用 `gh release create --generate-notes` 生成的是自动清单（只有提交标题），
> 把下面这份贴进 Release 的编辑框，替换掉自动内容。**每次发版必须包含"安装 / 校验 / 已知限制"三节**，
> 因为它们是用户下载后最先遇到的问题（SmartScreen、SHA256、数据在哪）。
>
> 复制后替换 `<...>` 占位符。SHA256 从 Release 附件里的 `SHA256SUMS.txt` 取（小写），
> 校验命令输出的十六进制是**大写**，写给人看时统一大写并注明。

---

## chaiai <version>

<一句话概括本版主题>

### 新增

- <功能，一句一项，带用户能感知到的结果>

### 修复

- <症状 → 现在怎样>

### 安装

1. 下载 `chaiai-setup-<version>.exe`
2. 双击安装（按用户安装，不需要管理员权限；可以改安装位置与数据目录）
3. 首次启动后在「设置 → 模型连接」里填你自己的端点与密钥

> **安装包没有代码签名**：Windows SmartScreen 会提示「未知发布者」，点「更多信息 → 仍要运行」。

卸载：设置 → 关于 → 卸载；也可以从开始菜单「卸载 chaiai」或 Windows 的「应用」。默认**不删**数据，
要连数据一起删需在程序内显式勾选。

### 校验下载

```powershell
Get-FileHash .\chaiai-setup-<version>.exe -Algorithm SHA256
# 期望（大写）：<SHA256-EXE>
```

绿色包：`Get-FileHash .\chaiai-<version>-win64.zip -Algorithm SHA256` → `<SHA256-ZIP>`

### 已知限制

- 早期版本（v0.x），接口与契约仍会变，**不建议作为生产依赖**
- 仅 Windows 10/11
- 没有代码签名，首次运行会被 SmartScreen 拦一次
- 检查更新只**提醒**，不会自动下载或安装；升级方式是下载新安装包覆盖安装
- 卸载默认不删数据

### 完整变更

`git log --oneline <上一个 tag>..<本 tag>`

---

## 发版检查清单（维护者用）

1. `git status` 干净；`pytest` 全绿；`ruff` / `mypy` / `check_size.py` / `check:api` 通过
2. 版本号：`pyproject.toml` 与 `src/local_agent/__init__.py` **同时**改（有测试断言两者一致）
3. `python scripts/check_release_contracts.py --check`（改过 API / 默认值后先 `--write` 再审查）
4. push 分支 → 打 tag → push tag（**注意 remote：`main` 是 GitHub，`origin` 是 Gitee**）
5. 等 Actions 出 Release，从**Release 附件**取 SHA256 填进上面的模板
6. 在本机装一次 Release 里的包，确认版本号、关于页、卸载都正常
