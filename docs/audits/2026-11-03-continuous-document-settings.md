# 连续文档阅读与设置页交互

2026-11-03。范围为用户截图中的文档连续向下阅读，以及用户确认的设置分类导航、搜索跳转、模型连接呈现和保存反馈。本轮只修改前端，不改配置存储协议、模型请求逻辑，不升级依赖，不重建 exe。

## 问题与结果

- 原阅读器只渲染当前页，滚轮无法跨页。现在全部页面以占位纸张保留在同一滚动区；滚轮连续阅读，页码随视口中部所在页更新。上一页/下一页作为滚动定位快捷键保留。
- 每页按实际尺寸排版，只在距视口约 700px 内绘制画布和文字层；离开后卸载这些 DOM，保留页面位置。缩放和改变面板宽度时，按顶部可见页及相对位置恢复滚动；页面加载失败可单独重试。
- 原设置导航点击只改分类，没有清空搜索，搜索结果会继续遮住分类内容。现在点击分类退出搜索；Esc 先清搜索，清空按钮返回输入焦点；搜索结果跳转后滚到实际字段并聚焦输入控件，遵循减少动态效果偏好。
- 分类保留各自滚动位置；偏好分区继续保持挂载，切换分类和搜索不会清除当前弹窗内的草稿。桌面分类支持方向键、Home/End；760px 以下使用分组下拉，避免长横向分类条占空间。
- 模型连接收起时隐藏技术 ID，名称、模型、默认状态及检测状态仍可见；ID 留在详情中。已有添加、编辑、测试、默认选择、停用确认和错误恢复流程保留。
- 表单固定操作区显示尚未保存的修改项数，保存/取消/恢复初始值沿用现有接口和状态。
- 已确认并修复一个导航遗漏：`PreferenceSection` 已提供 `consolidation`，导航分组也声明了它，但可见分区清单未登记。现在“记忆巩固”可从桌面分类、窄屏下拉和字段搜索进入；测试只导航，不启用后台调用。

## 文件

路径相对项目根目录。

| 文件 | 说明 |
| --- | --- |
| `local-agent-web/frontend/src/features/panel/DocumentView.tsx` | 连续页面、附近绘制、滚动页码、缩放位置及单页重试 |
| `local-agent-web/frontend/src/features/panel/PreviewPane.module.css` | 连续纸张占位、页间距、加载和错误展示 |
| `local-agent-web/frontend/src/features/settings/SettingsDialog.tsx` | 分类与搜索协调、滚动恢复、字段焦点、键盘导航、窄屏选择、连接 ID 收起及巩固入口 |
| `local-agent-web/frontend/src/features/settings/SettingsDialog.module.css` | 搜索清空按钮、窄屏分类选择和内容间距 |
| `local-agent-web/frontend/src/features/settings/PreferenceSection.tsx` | 按当前草稿基准计算未保存项数 |
| `local-agent-web/frontend/src/features/settings/PreferenceSection.module.css` | 未保存提示 |
| `local-agent-web/frontend/src/features/settings/SettingsDialog.test.tsx` | 搜索退出、Esc、分类键盘顺序、滚动/草稿及巩固入口回归 |
| `local-agent-web/frontend/e2e/work-panel.spec.ts` | 34 页连续滚轮、末页、页码、缩放与画布数量验收 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 四种尺寸下设置导航、草稿、搜索跳转和输入焦点验收 |
| `local-agent-web/frontend/e2e/fixtures/preferences.ts` | 浏览器测试偏好数据，不写用户设置 |
| `local-agent-web/frontend/e2e/fixtures/panel-long-report.pdf` | 重复两页中性样本组成的 34 页测试文件 |
| `docs/audits/2026-11-03-continuous-document-settings.md` | 本记录 |

## 验证

在 `local-agent-web/frontend` 执行：

```powershell
npm test -- --reporter=dot
npx playwright test -c playwright.preview.config.ts --max-failures=1
npx playwright test -c playwright.dsh.config.ts --max-failures=1
npx playwright test -c playwright.dsh.config.ts -g 'settings search' --max-failures=1
```

- 最终全量前端单元测试：73 个文件、735 项通过。
- 文件面板浏览器场景：63 项通过；手机全宽面板不显示鼠标拖宽手柄，1 项按设计跳过。
- 全局工作台浏览器场景：40 项通过。补充记忆巩固入口和真实字段聚焦断言后，四种尺寸下的设置交互用例再次全部通过。
- 尺寸为 1440×900、1280×800、768×900、390×844。没有替换截图基线。
- 长文档：实际滚轮跨页，缩放后阅读页保持在相邻页范围内，滚到末尾后第 34 页绘制完成；附近画布少于 10 个。测试不是把 34 张画布同时挂载后仅裁剪显示。
- 设置：搜索结果点击后“拒绝的域名”输入框获得焦点并处于视口内；分类切换退出搜索；偏好草稿在切换后仍保留；Esc 清搜索、清空按钮恢复搜索焦点；“记忆巩固”分类和搜索都可进入。
- 浏览器验收启动时执行 `npm run build`，包含 TypeScript 检查。修改文件 ESLint 无错误；`PreferenceSection.tsx` 原有 4 条共享常量导出的 Fast Refresh 警告保留。所改已跟踪文件 `git diff --check` 通过。
- 8731 首页返回 200，并引用已生成的前端构建资源。测试中所有设置数据由接口 mock 提供，没有保存真实用户配置，没有执行真实模型连接探测。

## 手动复核

1. 刷新 `http://127.0.0.1:8731/`，在工作面板打开长 PDF 或可转换的 Word 文档。将鼠标移入纸张区域，滚轮向下，应连续跨页并更新页码；放大后继续滚动到末页。
2. 打开设置，搜索“域名”，点击“拒绝的域名”，确认直接定位并聚焦。再次搜索后直接点击其他分类，确认显示该分类内容。
3. 在“通用与外观”修改文字大小但不保存，切到其他分类再返回，确认草稿和未保存提示仍在；点“取消修改”还原。缩窄窗口，使用顶部分类下拉切换。
4. 打开“记忆巩固”，确认分类入口和字段展示存在；无需启用开关或保存。

## 验收边界

- 34 页样本用于验证滚动、绘制范围和定位，不代表所有复杂 Office 文件的转换兼容性。真实截图文档、横竖页混排、超大扫描件仍未逐一验证。
- 草稿保留范围是当前打开的设置弹窗；本轮没有新增关闭弹窗或刷新后的草稿持久化。
- 搜索范围沿用偏好字段索引，不包含模型连接名称和权限授权路径。
- 本轮未重建 exe，也未单独验证 WebView2；没有运行后端测试。Office 转换链沿用前一轮实现。
- 截图保存在本次会话可视化目录：`continuous-document-desktop.png`、`continuous-document-mobile.png`、`settings-navigation-desktop.png`、`settings-navigation-mobile.png`。
