# 官网（website/）

对外展示用的静态站点：首页宣传页 + 文档站。纯 HTML/CSS，无构建、无外部依赖、无追踪脚本。

## 结构

```
website/
  site.html            首页（宣传页）
  docs/                文档站：入口 index.html + 操作手册 8 章 + internals/ 技术实现 8 章
    index.html         文档首页＝操作手册首页
    README.md          文档写作规范（维护者看，部署时可排除）
    assets/docs.css    文档站样式
  README.md            本文件（维护者看，部署时可排除）
```

## 本地预览

- 直接双击 `site.html` 即可（文档链接用相对路径，`file://` 下可用）。
- 想按服务器的方式预览：在本目录执行 `python -m http.server 8080`，然后打开 http://127.0.0.1:8080/site.html 。

## 部署

把整个 `website/` 目录拷到任意静态服务器的站点根目录即可，入口是 `site.html`。

- 如果需要用 `https://域名/` 直接打开，可在服务器上配置默认文档为 `site.html`；
  若坚持把 `site.html` 改名为 `index.html`，必须同时把 `docs/` 下**所有页面**里的 `../site.html` 改成 `../index.html`
  （每页 3 处：顶栏品牌、顶栏「返回官网」、侧栏页脚），漏一页就会有一个 404。
- `README.md` 与 `docs/README.md` 是维护文档，可不发布。
- 站点不依赖后端，也不调用 `127.0.0.1:8000` 的任何接口。

## 文档

文档的写作口径、章节约定、页面骨架与自查清单见 [`docs/README.md`](docs/README.md)。
两套材料：操作手册八章（`docs/index.html` 为首页，`docs/01…08-*.html` 为章节）与 `docs/internals/index.html`（面向开发者，8 个章节页），均已写完。
左栏是全站统一的一棵树，不再需要"先进文档首页、再点操作手册"。
