# 对外文档规范（website/docs/）

官网由 `website/` 统一管理：`site.html` 是首页，`docs/` 是文档站。两者都手写静态 HTML，
挂到任意静态服务器即可。仓库里的 `docs/`（audits / lessons / release）是**内部记录**，
不对外发布，两处不要互相链接。

## 1. 形态：多页手写 HTML，不做构建

- 每页一个完整静态 HTML，样式只引 `../assets/docs.css`，没有 JS、没有外部字体与图标。
- **一页一章**：正文超过约 150 行、或者开始出现两个以上并列主题时，就拆成新页。
  长页面读者会丢失位置，侧栏也应该跟着长出来，而不是把一篇文章越堆越长。
- 代价：新增一章要同步多处侧栏（见第 6 节），这是不用构建链的必然成本。

## 2. 目录

```
website/
  site.html                官网首页
  docs/
    README.md              本规范（维护者看，发布时可排除）
    index.html             文档概览：两个入口
    manual/                操作手册（面向使用者）
      index.html           手册首页：8 章目录 + 一张总览图
      01-install.html      安装与第一次启动
      02-model.html        连接一个模型
      03-permissions.html  权限三档与目录授权
      04-rounds.html       聊天、Agent 与一轮任务
      05-map.html          会话地图与分支追问
      06-data.html         数据中台
      07-settings.html     设置项与生效时机
      08-faq.html          常见问题与自检
    internals/             技术实现说明（面向开发者）
      index.html           概览 + 8 章目录（正文待编写）
    assets/
      docs.css             唯一样式表
      img/                 截图与示意图
```

命名：`<两位序号>-<kebab-case>.html`。序号只在目录页与文件里用，标题里也保留，便于交叉引用。

## 3. 两种页面骨架

### 章节页（`layout--plain`：左栏 + 正文，没有右栏）

```html
<div class="layout layout--plain">
  <nav class="sidebar"> …见第 4 节… </nav>
  <main class="doc">
    <p class="kicker">MANUAL · 第 1 章</p>
    <h1>章节标题</h1>
    <p class="lead">这一章解决什么。</p>
    <div class="prose"> …h2 分节、表格、插图… </div>
    <nav class="pager"> 上一篇 / 下一篇 </nav>
    <p class="pagefoot">最后更新 YYYY-MM-DD · 适用版本 v1.0</p>
  </main>
</div>
```

### 概览页（`layout`：左栏 + 正文 + 右侧「本页目录」）

概览页才有右栏，锚点只列这一页的 `h2`。章节目录用 `ol.chapter-list`，一条一章。

## 4. 左栏树：新增一章时要同步的地方

左栏是**每个页面各写一份**（无 JS），结构固定：

```html
<nav class="sidebar" aria-label="文档目录">
  <div class="sidebar__group">
    <p class="sidebar__title">文档</p>
    <a href="../index.html">概览</a>
  </div>
  <div class="sidebar__group">
    <p class="sidebar__title">操作手册</p>
    <a href="index.html">手册首页</a>
    <a href="01-install.html" aria-current="page">01 安装与第一次启动</a>
    …其余章节…
  </div>
  <div class="sidebar__group">
    <p class="sidebar__title">技术实现说明</p>
    <a href="../internals/index.html">概览</a>
  </div>
  <div class="sidebar__foot"><a href="../../site.html">← 返回官网</a></div>
</nav>
```

新增一章需要改：① 新建章节页；② `manual/index.html` 的章节列表与下一条 pager；③ 相邻两章的 pager；
④ **所有文档页的侧栏**（当前 11 个页面）；⑤ 章节页顶栏 `aria-current`。漏掉 ④ 会让侧栏在别的页面里少一条。

## 5. 图与图注

- 截图必须来自 **fixture 场景**（`http://127.0.0.1:8000/?state=<场景>`），数据是虚构的；
  禁止截真实会话、真实路径、真实密钥或真实用量。
- 概念图用自绘 SVG（如分支结构），不要用旧截图硬凑。
- 命名：`ui-<场景>-YYYYMMDD.png`、`<主题>-YYYYMMDD.svg`；放在 `assets/img/`。
- 写法：

```html
<figure>
  <img src="../assets/img/ui-approval-20261019.png" alt="审批卡：待审批的高风险删除"
       width="1440" height="900" loading="lazy" />
  <figcaption>高风险删除的审批卡：第一次确认只进入二次确认，不会直接删。</figcaption>
</figure>
```

- 图注要写"看到什么"，不要写"如图所示"。截图里的示例数据，在图注里点明"示意数据"。
- 一页 1–3 张图为宜；图多不等于讲得清。

## 6. 写作口径

**要写清的**

- 能做 / 不能做：每条能力给出边界，例如"外部路径默认可读，但写入要逐目录授权"。
- 已验证 / 未验证：没实测过的结论标 **未验证**，并写出"怎么验证"。
- 代码引用：技术实现的结论必须能指到 `文件:行号`。
- 操作步骤：命令 + 预期结果 + 失败时先查哪里。

**不要写的**

- 本机绝对路径、会话 id、真实密钥、审计记录原文。
- 未实现的规划（"将要支持…"）。
- 没有依据的"通常 / 应该没问题"；拿不准就写"不确定，验证方法是…"。

**术语（全站统一）**

| 词 | 含义 |
|---|---|
| 工作区 | 会话绑定的目录，默认上下文、相对路径与检索的根 |
| 轮次 | 一次提问到本轮结束（含工具、审批、用量） |
| 档位 | 权限三档：只读 / 工作区可写 / 常规免审 |
| 审批 | 有副作用的动作执行前的确认卡 |
| 投影 | 服务端把运行状态整理成页面可读的只读视图 |
| 未验证 | 没有实测证据，只有代码或推断支撑 |

术语表放在 `manual/index.html` 的「附：术语」，两套材料共用；新增术语先加表，再在正文里沿用。

## 7. 以后迁 Markdown 的约定

保持四件事不变，迁移就是"HTML 标签换 Markdown 符号"：

1. 每页一个 `h1`，`lead` 是 `h1` 后的第一段；
2. 分节只用 `h2`（编号保留），不跳级；
3. 提示块统一写成引用块 `> **注意：** …`；
4. 代码块标语言（`bash`、`python`、`text`），路径写仓库相对路径。

## 8. 提交前自查

- [ ] 直接双击 `site.html` 与各文档页（`file://`）样式正常，控制台无报错；
- [ ] 1440 / 1024 / 390 三个宽度下正文可读，表格与代码块不撑破页面；
- [ ] 所有链接可点：不出现 404、不出现以 `/` 开头的路径；
- [ ] 新章节已同步所有页面的侧栏，`aria-current` 每页只有一处；
- [ ] 上一篇 / 下一篇指向正确；
- [ ] 图片都是 fixture 截图或自绘，带 `alt`、宽高与图注，图注写明"示意数据"；
- [ ] 没有内联 `style`（样式只改 `assets/docs.css`）；
- [ ] 没有外部请求、没有本机绝对路径 / 密钥 / 会话 id；
- [ ] "最后更新"日期已改。
