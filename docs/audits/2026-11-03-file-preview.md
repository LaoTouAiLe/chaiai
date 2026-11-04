# DSH 文件栏与分类预览

本轮参考本机 `deepseek-harness` 的 `ui-sidebar-documentpreview`，沿用其固定工具条、独立滚动区、纸张背景、代码/排版切换和分类渲染思路。DSH 的 MIT 声明已保存在 `docs/licenses/deepseek-harness-MIT.txt`。本轮没有重建 exe。

## 实现

- `local-agent-web/frontend/src/features/panel/PreviewPane.tsx`、样式：38px 工具条、图标关闭、刷新、Markdown 排版/源码切换、隔离 HTML 预览、图片缩放；复制按钮明确区分全文和已加载内容。
- `DocumentView.tsx`：本地 PDF.js 阅读器，Word/PPT 转换后沿用同一阅读器；支持上一页/下一页、适应宽度、缩放、文字选择，独立滚动、纸张阴影与加载/失败重试状态。
- `SpreadsheetView.tsx`：XLSX 行列网格、固定表头/行号、工作表标签、每组 200 行的分页，不把表格显示成代码。
- `PanelShell.tsx`、样式：右侧文件树默认 200px，可拖动、收起和展开；保留附件预览不展示工作区树的行为。预览按文件路径重新挂载，避免旧文件的异步分页结果污染新文件。
- `local-agent-web/src/local_agent/application/document_preview.py`：独立文档转换、内容缓存、并发限制、工作簿按工作表和行分页。转换只读取经过路径校验的文件，把私有副本交给转换引擎。
- `artifacts.py`、`service.py`、`api/routes.py`、`data_center_models.py`：增加 `presentation=formatted` 的视觉预览和受工作区/附件根约束的 PDF 内容端点；原有文本预览继续可用。同步 OpenAPI 和前端类型。
- `local-agent-web/preview-runtime/`：使用 DSH 相同版本的 `@deepseek-ai/libreoffice-kit@0.1.5`，独立安装 Windows x64 引擎与 Koffi 原生模块，不依赖 DSH 目录。前端使用 `pdfjs-dist@6.3.289`。

## 源码运行准备

Windows x64，Node 22.20+，在 `local-agent-web/preview-runtime` 执行：

```powershell
npm ci --omit=optional --ignore-scripts
```

Windows 引擎与 Koffi Windows 二进制作为显式依赖安装。普通 `--omit=optional` 会省略 Koffi 所需的可选平台包，因此显式声明该平台包；它已提供预编译二进制，不需要本机 CMake 编译。

前端从 `local-agent-web/frontend` 执行 `npm ci`、`npm run build`。部署时需要保留 `preview-runtime` 的脚本和运行依赖，以及可用的 Node；本轮未修改安装包脚本。

## 验证记录

- 后端 `test_open_in_app.py`、`test_preview_paging.py`、新增 `test_visual_preview.py`：28 项通过。
- 修正原分页测试夹具：DOCX 段落不是物理页，改用七页 PDF 验证页窗。
- 前端面板相关单元测试最终复核：48 项全部通过；附件恢复测试同步验证显式请求 formatted 预览。
- 已实测原生转换：自建含中文标题、段落和表格的 Word → 52541 字节、2 页 PDF；PPTX → 24637 字节、1 页；旧 DOC → 26791 字节、1 页；旧 PPT → 27012 字节、1 页；旧 XLS → 24627 字节、1 页。输出均能由 PDF 解析器读取。
- 浏览器用例在 `frontend/e2e/work-panel.spec.ts` 增加 Office/PDF、Markdown/HTML、工作簿场景；`playwright.preview.config.ts` 使用生产构建，在桌面和手机尺寸执行，不更新旧截图基线。
- 实机转换输入及输出在 `.playwright-cli/visual-preview/`；浏览器截图由 Playwright 输出到 `frontend/test-results/`。

## 明确边界

- 当前实现借鉴 DSH 的主要预览结构，并不声称移植了 DSH 全部功能。
- XLSX 网格展示单元格值与工作表，尚未复刻 Excel 的复杂合并、图表、条件格式和编辑；旧 XLS 通过 PDF 版式展示。
- HTML 预览隔离脚本和网络资源，只展示文件内的静态 HTML/CSS；不执行页面脚本。
- Office/PDF 输入和转换输出上限为 32 MB；转换并发 2，缓存最多 8 份且总量不超过 64 MB。表格每组 200 行、最多展示 200 列，超列时明确提示。
- Office 格式还原依赖本机字体；本轮只验证了上述样本，没有验证所有加密、损坏及复杂排版文档。
- 源码版本已安装运行时；exe/WebView2 与安装包内的引擎分发尚未验证，也没有重建 exe。

## 最终浏览器与接口复核

- `npx playwright test -c playwright.preview.config.ts --project=1440x900 --project=390x844 --max-failures=1`：27 项通过，1 项按设计跳过手机鼠标拖宽，56.7s；生产构建、类型检查通过。
- 新增真实 HTTP 接口测试：1 项通过，验证格式化元数据、PDF 字节响应、禁止缓存、工作区外 403、不存在 404、无效 PDF 422。后端合计本轮 29 项通过。
- 初轮新增浏览器用例因文件列表缓存和文件行可访问名包含大小而等待失败；调整夹具注入时机和选择器后，上述完整复核通过。没有改写截图基线。
- 原改动页用例先验证桌面拖宽再验证手机堆叠布局，补齐其初始桌面视口，避免在手机项目中错误地断言左右布局。
- 已检查桌面 Office 和表格截图：Word 使用真实转换样本，中文标题、段落和表格均出现在白色纸张上；表格显示独立工作表标签和行列标题。
- PDF 阅读器实际图片可见并可翻页缩放；文字选择层已接入。未单独自动化验证跨页文字选择或复杂字体的选择位置。
- Ruff、ESLint、`git diff --check` 均通过；生产构建保留原有大 chunk 提示。未执行整个后端或整个前端测试集。

## 手动验收

重启后端以加载新接口。右侧文件栏分别打开 DOCX、DOC、PPTX、PDF、XLSX、Markdown 与 HTML：检查文档翻页/缩放、工作表切换与下一组行、预览/源码切换和文件树收起/展开。打开方式沿用已有入口；本轮没有自动替用户启动 IDE。
