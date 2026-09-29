# 设计 Token（浅色主题，T25 冻结）

来源与冻结版本见 [README.md](README.md)。原始证据：

- `tokens/tokens-declared.txt`：423 条 `--dsw-*` 声明的原文（从运行页面注入的 `<style>` 中提取）。
- `tokens/tokens-light-resolved.txt`：350 条变量在**浅色主题下解析后**的取值（从 `#root` 的
  `getComputedStyle` 读取，`var()` 已替换）。

## 1. 参考项目的三层结构

```text
--dsw-static-*      调色板：主题无关的原始色阶（…-00 … -1000）
        ↓ 引用
--dsw-alias-*       语义层：随主题切换，同一条声明在浅/深两套主题里各出现一次
        ↓ 引用
--dsw-specific-*    业务专有：气泡、侧栏、输入区、菜单等具体 surface
```

另有与主题无关的字体阶梯 `--dsw-font-*`、阴影 `--dsw-shadow-*`、渐变 `--dsw-linear-*`。
上游还提供 `--ds-font-family-code`、`--ds-ease-in-out`、`--ds-transition-duration*`
（定义在 `packages/client/ui-theme/src/styles/base.css`）。

**命名空间的启示**：Token 名与语义绑定（`bg-layer-1` / `label-secondary` / `state-warn-primary`），
不绑定具体组件。本项目 T26 应对齐这套**命名习惯**，但使用自己的 CSS 变量前缀；下面的数值是
"视觉等价"的基准，不是必须逐字节相同的常量。

## 2. 语义 Token：浅色主题解析值（权威）

以下为采集到的实际取值（十六进制已归一）。

### 背景与遮罩

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-bg-base` | `#ffffff` | 应用底色 |
| `--dsw-alias-bg-layer-1` | `#ffffff` | 一级面（内容区） |
| `--dsw-alias-bg-layer-2` | `#ffffff` | 二级面（卡片） |
| `--dsw-alias-bg-layer-3` | `#ffffff` | 三级面（菜单/浮层） |
| `--dsw-alias-bg-overlay` | `#e9ecf2` | 悬浮层底 |
| `--dsw-alias-bg-module-platform` | `#f5f6f7` | 模块容器 |
| `--dsw-alias-bg-multi-select` | `#f5f6f7` | 多选态 |
| `--dsw-alias-bg-skeleton` | `#0000000a` | 骨架屏 |
| `--dsw-alias-bg-mask-1` | `#0000003d` | 主遮罩 |
| `--dsw-alias-bg-mask-2` | `#0000001f` | 轻遮罩 |
| `--dsw-alias-bg-mask-3` | `#0000007a` | 重遮罩 |
| `--dsw-alias-bg-mask-photo` | `#000000e0` | 图片查看底色 |
| `--dsw-alias-bg-mask-drop` | `#ffffffb3` | 拖放提示层 |

### 边框

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-border-l1` | `#0000000a` | 最弱分隔 |
| `--dsw-alias-border-l2` | `#0000001a` | 常规描边 |
| `--dsw-alias-border-l2-darkmode-thin` | `#0000001a` | 暗色下减薄 |
| `--dsw-alias-border-l3` | `#0000001f` | 强调描边 |
| `--dsw-alias-border-l4` | `#00000029` | 最强描边 |

### 文本

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-label-primary` | `#0f1115` | 正文主色（`#root` 实测一致） |
| `--dsw-alias-label-primary-foreground` | `#ffffff` | 深底上的前景 |
| `--dsw-alias-label-primary-dimmed` | `#151517` | 主色弱化 |
| `--dsw-alias-label-primary-inverted` | `#ffffff` | 反色 |
| `--dsw-alias-label-primary-bluish` | `#0e3074` | 蓝调主色 |
| `--dsw-alias-label-secondary` | `#61666b` | 次级文本（上下文计量器实测一致） |
| `--dsw-alias-label-tertiary` | `#81858c` | 三级文本 |
| `--dsw-alias-label-caption` | `#adb2b8` | 说明/时间戳 |
| `--dsw-alias-label-dimmed` | `#e1e5ee` | 不可用 |

### 按钮

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-button-primary-fill` | `#0f1115` | 主按钮底（深色填充） |
| `--dsw-alias-button-primary-hover` | `#43454a` | 主按钮悬停 |
| `--dsw-alias-button-primary-dimmed` | `#ebeef2` | 主按钮禁用 |
| `--dsw-alias-button-elevated-fill` | `#ffffff` | 浮起按钮底 |
| `--dsw-alias-button-floating-fill` | `#ffffff` | 悬浮按钮底 |
| `--dsw-alias-button-floating-hover` | `#f1f3f5` | 悬浮按钮悬停 |
| `--dsw-alias-button-contrast-fill` | `#61666b` | 对比按钮 |
| `--dsw-alias-button-ghost-active-fill` | `#ebeef2` | 幽灵按钮激活底 |
| `--dsw-alias-button-ghost-active-hover` | `#e9ecf2` | 幽灵按钮激活悬停 |
| `--dsw-alias-button-ghost-active-border` | `#979da6` | 幽灵按钮激活描边 |
| `--dsw-alias-button-info-fill` | `#4176e6` | 信息按钮 |
| `--dsw-alias-button-info-hover` | `#679efe` | 信息按钮悬停 |
| `--dsw-alias-button-tool-bar-fill` | `#54555780` | 浮动工具条底 |

### 交互态

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-interactive-bg-hover` | `#2631480f` | 列表项悬停 |
| `--dsw-alias-interactive-bg-hover-solid` | `#f1f3f5` | 悬停（不透明） |
| `--dsw-alias-interactive-bg-active` | `#2631481a` | 选中 |
| `--dsw-alias-interactive-bg-hover-accent` | `#26314824` | 强调悬停 |
| `--dsw-alias-interactive-bg-hover-danger` | `#ec13130d` | 危险悬停 |

### 状态色

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-state-success-primary` | `#22c55e` | 成功 |
| `--dsw-alias-state-success-secondary` | `#4ed17e` | 成功浅 |
| `--dsw-alias-state-success-tertiary` | `#e6faed` | 成功底 |
| `--dsw-alias-state-warn-primary` | `#f59e0b` | 警告（审批卡主色） |
| `--dsw-alias-state-warn-secondary` | `#f7ad31` | 警告浅 |
| `--dsw-alias-state-warn-label` | `#dd8629` | 警告文字 |
| `--dsw-alias-state-warn-tertiary` | `#fef5e7` | 警告底（审批卡底） |
| `--dsw-alias-state-error-primary` | `#ec1313` | 错误 |
| `--dsw-alias-state-error-secondary` | `#f25a5a` | 错误浅（失败行） |
| `--dsw-alias-state-business-primary` | `#4176e6` | 业务蓝 |
| `--dsw-alias-state-business-tertiary` | `#e4edfd` | 业务蓝底 |

### 业务专有面

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-specific-sidebar-fill` | `#f9fafb` | 侧栏底色 |
| `--dsw-specific-sidebar-nav-item-hover` | `#f1f3f5` | 侧栏项悬停 |
| `--dsw-specific-sidebar-nav-item-active` | `#ebeef2` | 侧栏项选中 |
| `--dsw-specific-sidebar-nav-item-active-accent` | `#e4edfd` | 侧栏项选中强调 |
| `--dsw-specific-bubble` | `#edf3fe` | 用户气泡底 |
| `--dsw-specific-bubble-highlight` | `#d3e2ff` | 气泡高亮 |
| `--dsw-specific-input-major` | `#ffffff` | 主输入区底 |
| `--dsw-specific-selector` | `#f5f6f7` | 选择器底 |
| `--dsw-specific-menu` | `#ffffff` | 菜单底 |
| `--dsw-specific-tip` | `#f5f6f7` | 提示块底 |
| `--dsw-alias-tooltip-bg` | `#2c2c2e` | 提示浮层（深底） |
| `--dsw-alias-toast-bg` | `#353638` | Toast（深底） |

### 滚动条

| Token | 浅色取值 |
|---|---|
| `--dsw-alias-scrollbar-bg-l1` / `l2` | `#e5e5e5` |
| `--dsw-alias-scrollbar-hover-l1` / `l2` | `#d4d4d4` |

### Markdown

| Token | 浅色取值 | 用途 |
|---|---|---|
| `--dsw-alias-markdown-code-block` | `#f9fafb` | 代码块底 |
| `--dsw-alias-markdown-code-block-banner` | `#f9fafb` | 代码块标题条 |
| `--dsw-alias-markdown-inline-code` | `#ebeef2` | 行内代码 |
| `--dsw-alias-markdown-citation` | `#ebeef2` | 引用 |
| `--dsw-alias-markdown-tag` | `#f1f3f5` | 标签 |
| `--dsw-alias-markdown-placeholder` | `#f5f6f7` | 占位块 |
| `--dsw-alias-markdown-code-segment-selected` | `#ffffff` | 代码段选中 |
| `--dsw-alias-markdown-code-segment-unselected` | `#f1f3f5` | 代码段未选中 |

## 3. 调色板（`--dsw-static-*`）

原始色阶在浅/深两套主题各声明一次；下表为采集到的取值，**双值条目表示两套主题取值不同**。
浅色主题实际生效的取值已在上面的语义表中体现，本节仅用于理解色阶跨度。

| 色族 | 色阶 |
|---|---|
| neutral-bluish | 00 `#ffffff` / 50 `#f9fafb` / 60 `#f5f6f7` 或 `#f9fafb` / 75 `#f1f3f5` / 100 `#ebeef2` / 150 `#e9ecf2` / 200 `#e1e5ee` / 300 `#cfd3d6` / 400 `#adb2b8` / 500 `#979da6` / 600 `#81858c` / 700 `#61666b` / 750 `#43454a` / 800 `#353638` / 850 `#2c2c2e` / 875 `#232324` / 900 `#1b1b1c` / 950 `#151517` / 1000 `#0f1115` |
| neutral | 00 `#ffffff` / 50 `#fafafa` / 100 `#f5f5f5` / 150 `#ededed` / 200 `#e5e5e5` / 250 `#dcdcdc` / 300 `#d4d4d4` / 400 `#a2a4a6` / 500 `#7f8287` / 550 `#65676b` / 600 `#545557` / 700 `#3c3c3d` / 800 `#292929` / 850 `#212123` / 900 `#0f0f0f` / 1000 `#000000` |
| deepseek（参考方品牌蓝） | 50 `#edf3fe` / 100 `#e4edfd` / 200 `#d3e2ff` / 300 `#b7c8fe` / 400 `#679efe` / 450 `#5686fe` / 500 `#4176e6` / 600 `#4868b2` / 700 `#2f4c8f` / 800 `#34415b` / 900 `#283142` |
| blue | 50 `#eff6ff` / 50p `#eaf3ff` / 75 `#e5f0ff` / 100 `#dbeafe` / 300 `#93c5fd` / 400 `#60a5fa` / 450 `#4d93f8` / 500 `#3b82f6` / 600 `#2563eb` / 800 `#1e40af` / 900 `#0e3074` / 950 `#172554` |
| red | 50 `#fef2f2` / 100 `#fee2e2` / 400 `#f25a5a` / 500 `#ef4444` / 600 `#ec1313` |
| green | 100 `#e6faed` / 400 `#4ed17e` / 500 `#22c55e` / 900 `#233c2c` |
| amber | 100 `#fef5e7` / 400 `#f7ad31` / 500 `#f59e0b` / 600 `#dd8629` / 900 `#27241f` |

> 品牌色说明：`deepseek-*` 是参考产品的品牌色阶（`#4176e6` 为其品牌主蓝）。本项目 v0.4 沿用该色阶的
> **视觉层级关系**（主蓝＝业务强调色），但不把它当作本项目的品牌资产；若后续确定自身品牌色，
> 只需替换 `state-business-*` / `button-info-*` 一组引用。

## 4. 字体

### 字体族

```text
--dsw-font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif;
--ds-font-family-code: 'SF Mono', 'JetBrains Mono', 'Fira Code', Consolas,
  'Liberation Mono', Menlo, Courier, 'PingFang SC', 'Microsoft YaHei';
```

`base.css` 注释说明：代码字体**故意不加裸 `monospace` 兜底**——Windows 下 CJK 会掉到 SimSun。
本项目应沿用这一结论。

### 基础阶梯（`--dsw-font-<name>`，`-strong-` 表示字重 500）

| 名称 | 字号 / 行高 | 字重 |
|---|---|---|
| `xxxs-11` / `xxxs-strong-11` | 11px / 14px | 400 / 500 |
| `xxs-12` / `xxs-strong-12` | 12px / 18px | 400 / 500 |
| `xs-13` / `xs-strong-13` | 13px / 20px | 400 / 500 |
| `s-14` / `s-strong-14` | 14px / 22px | 400 / 500 |
| `base-16` / `base-strong-16` | 16px / 24px | 400 / 500 |
| `m-18` | 16px / 28px | 500 |
| `l-20` | 20px / 28px | 500 |
| `xl-24` | 24px / 32px | 600 |

### Markdown 阶梯

| 名称 | 字号 / 行高 | 字重 |
|---|---|---|
| `markdown-small[-strong]` | 14px / 24px | 400 / 600 |
| `markdown-base[-strong]` | 16px / 28px | 400 / 600 |
| `markdown-code` | 14px / 22px | 400（代码族） |
| `markdown-code-block` | 13px / 22px | 400（代码族） |
| `markdown-code-block-small` | 12px / 18px | 400（代码族） |
| `markdown-table` / `markdown-table-head` | 15px / 25px | 400 / 500 |
| `markdown-h1` | 24px / 34px | 700 |
| `markdown-h2` | 22px / 32px | 700 |
| `markdown-h3` | 20px / 30px | 700 |
| `markdown-h4` | 16px / 28px | 600 |

正文默认（`body` / `#root` 实测）：字号 16px，字重 400，字体族同 `--dsw-font-family`。

## 5. 阴影与渐变

| Token | 值 |
|---|---|
| `--dsw-shadow-lv1-blur` | `0 4px 12px 0 #00000005` |
| `--dsw-shadow-lv1` | `0 2px 4px 0 #0000000d` |
| `--dsw-shadow-lv2` | `0 4px 12px 0 #00000005, 0 2px 8px 0 #0000000a` |
| `--dsw-shadow-lv3` | `0 0 1px 0 #0003, 0 0 4px 0 #00000005, 0 12px 32px 0 #00000014` |
| `--dsw-linear-gradient-think` | `linear-gradient(180deg, #fff 20.19%, #ffffff00 100%)`（浅色） |
| `--dsw-linear-think-select` | `linear-gradient(180deg, #f5f6f7 20.19%, #f5f6f700 100%)` |

用途对应（实测验证）：`lv2` = 输入区/浮动卡；`lv3` = 设置弹窗等大浮层。

## 6. 动效

```text
--ds-ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);
--ds-transition-duration: 0.2s;
--ds-transition-duration-fast: 0.1s;
--ds-transition-duration-slow: 0.3s;
```

来源：`packages/client/ui-theme/src/styles/base.css`。本项目 T26 的 Playwright 基线固定
`reduced-motion`；动画时长只在交互手测时核对。

## 7. 参考项目**没有** Token 化的维度（实测值）

参考的 Token 体系只覆盖颜色 / 字体 / 阴影 / 渐变，**没有** `radius` / `spacing` / `size` 变量：
圆角与间距都写在各自组件的 CSS 里。因此这些值只能实测，下表为 2026-09-23 在
1440×900 浅色主题下的测量结果（原始记录见采集脚本输出）：

| 对象 | 实测 |
|---|---|
| 应用根 `#root` | 1440×900，文本色 `rgb(15,17,21)` |
| 侧栏容器 | 宽 268px |
| 会话行 | 256×34px，圆角 8px，左右内边距 8px，项间距 6px，字号 14px |
| 顶栏（含页签） | 高 63px，下边框 1px，内边距 12px / 28px / 20px |
| 工具行（折叠态） | 高 24px，横向 flex |
| 输入区容器 | 圆角 **22px**，1px 描边 `rgba(0,0,0,0.1)`，阴影 = `lv2`，上内边距 10px，内部间距 12px，字号 16px/24px |
| 输入区文本框 | 内边距 4px / 12px / 16px，字号 16px/24px |
| 上下文计量器 | 28×28px，圆角 999px（正圆），图标/文字色 `#61666b` |
| 设置弹窗 | 800×800px，圆角 **24px**，阴影 = `lv3`，内边距 0（内部自管） |
| 设置左侧导航项 | 164×40px，圆角 **12px**，选中底 `#ebeef2`，字号 14px/22px |

> 未实测成功的目标（本轮采集未取到，T26 需要在实现时自行确定或补测）：用户气泡的行高与内边距、
> 助手正文的动作条高度、每轮统计行的字号、审批卡的内边距与圆角、发送按钮尺寸。

## 8. 本项目引用规则（T26 起生效）

1. 颜色、字体、阴影、动效按**语义**引用，不按组件名引用；新增颜色必须先判断能否落在既有语义槽位。
2. 圆角/间距/尺寸按第 7 节的实测值建立本项目自己的 `--law-space-*` / `--law-radius-*` 尺度，
   不逐组件写magic number。
3. 品牌色与标识使用本项目自身资产；参考方品牌色阶只用于确定层级关系。
4. Token 文件名与类别固定，Playwright 视觉基线在 T26 冻结后不得静默更新。
