"""移动访问的管理页（架构 §24；2026-11-03）。

**为什么是网关自己渲染的页面，而不是塞进 React 设置页**：前端跑在主服务的 origin
（``127.0.0.1:8731``），网关在另一个端口。集成进设置页就得开 CORS、或者让主服务多一条转发
路由——前者放宽了同源边界，后者让"主服务不为网关开例外"那条契约变形。这个页面只在本机用，
自带样式与脚本，不引任何外部资源。

**这个页面本身也受回环判定保护**：非本机 Host 打开会 403（见 ``gateway.is_local_admin``）。
一个能看到"生成配对码"和"撤销设备"的页面，本身就是提权入口。

页面用普通字符串而不是 f-string：里面有大量 CSS 与 JS 花括号，f-string 要写双花括号，
既容易写错又难读。所有动态内容都由页面自己向后端取。
"""

from __future__ import annotations

_PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>chaiai 移动访问</title>
<style>
 :root { --bg:#faf9f7; --card:#fff; --line:#e7e2dc; --ink:#1f1d1a; --dim:#6b6560; --accent:#c2571a; }
 * { box-sizing: border-box; }
 body {
   margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
   font: 15px/1.6 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
 }
 main { max-width: 44rem; margin: 0 auto; }
 h1 { font-size: 1.35rem; margin: 0 0 .35rem; }
 h2 { font-size: 1rem; margin: 2rem 0 .75rem; }
 .sub { color: var(--dim); margin: 0 0 1.5rem; }
 .card { background: var(--card); border: 1px solid var(--line); border-radius: .75rem; padding: 1.25rem; }
 .row { display: flex; gap: .75rem; flex-wrap: wrap; align-items: center; justify-content: space-between; }
 button {
   font: inherit; padding: .55rem 1rem; border-radius: .5rem; border: 1px solid var(--line);
   background: #fff; cursor: pointer;
 }
 button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
 button.danger { color: #a3312a; border-color: #e3c9c6; }
 button:disabled { opacity: .5; cursor: default; }
 code {
   font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .85em;
   background: #f3efea; padding: .1rem .35rem; border-radius: .25rem; word-break: break-all;
 }
 .qr { margin: 1rem 0 0; display: none; }
 .qr.on { display: block; }
 .qr .pic { background: #fff; border: 1px solid var(--line); border-radius: .5rem; padding: 12px; width: max-content; }
 .qr .pic svg { display: block; width: 220px; height: 220px; }
 .qr .meta { color: var(--dim); font-size: .9rem; margin-top: .75rem; }
 .qr .link { margin-top: .5rem; font-size: .9rem; }
 ul.devices { list-style: none; margin: 0; padding: 0; }
 ul.devices li {
   display: flex; justify-content: space-between; align-items: center; gap: 1rem;
   padding: .7rem 0; border-top: 1px solid var(--line);
 }
 ul.devices li:first-child { border-top: 0; }
 .dname { font-weight: 600; }
 .dmeta { color: var(--dim); font-size: .85rem; }
 .empty { color: var(--dim); }
 .warn { color: #a3312a; font-size: .9rem; margin-top: .75rem; min-height: 1.2em; }
 footer { margin-top: 2rem; color: var(--dim); font-size: .85rem; }
</style></head>
<body><main>
<h1>移动访问</h1>
<p class="sub">手机与这台电脑配对后，就能在手机上查看进度、继续对话。本页只能在本机打开。</p>

<div class="card">
  <div class="row">
    <div>
      <div class="dname">配对一台新手机</div>
      <div class="dmeta" id="gw-line">正在读取网关状态…</div>
    </div>
    <button class="primary" id="gen">生成配对二维码</button>
  </div>
  <div class="qr" id="qrpane">
    <div class="pic" id="qrpic"></div>
    <div class="meta" id="qrttl"></div>
    <div class="link">手机扫上面这个，或在手机浏览器打开：<br><code id="qrurl"></code></div>
  </div>
  <div class="warn" id="err"></div>
</div>

<h2>已配对的设备</h2>
<div class="card"><ul class="devices" id="devices"><li class="empty">读取中…</li></ul></div>

<footer>撤销立即生效：那台手机的下一次请求就会被拒绝。手机丢失请立刻在这里删除。</footer>
</main>
<script>
var el = function (id) { return document.getElementById(id); };
var timer = null;

function fmt(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

function loadStatus() {
  fetch('/__mobile/status')
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      el('gw-line').textContent = data.phone_url
        ? '手机访问 ' + data.phone_url + ' · 已配对 ' + data.devices.length + ' 台'
        : '还没有启用手机可达的通道（局域网或远程），手机暂时连不上';
      renderDevices(data.devices);
    })
    .catch(function (e) { el('gw-line').textContent = '读取状态失败：' + e.message; });
}

function renderDevices(list) {
  var box = el('devices');
  if (!list.length) {
    box.innerHTML = '<li class="empty">还没有配对的设备</li>';
    return;
  }
  box.innerHTML = '';
  list.forEach(function (d) {
    var li = document.createElement('li');
    var left = document.createElement('div');
    var name = document.createElement('div');
    name.className = 'dname';
    name.textContent = d.label || '未命名设备';
    var meta = document.createElement('div');
    meta.className = 'dmeta';
    meta.textContent = (d.channel || '未知通道') + ' · 配对于 ' + fmt(d.created_at);
    left.appendChild(name);
    left.appendChild(meta);

    var btn = document.createElement('button');
    btn.className = 'danger';
    btn.textContent = '删除';
    btn.onclick = function () {
      if (!confirm('删除后这台手机立即失去访问权限，确定？')) return;
      btn.disabled = true;
      fetch('/__mobile/devices/' + encodeURIComponent(d.id), { method: 'DELETE' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); loadStatus(); })
        .catch(function (e) { btn.disabled = false; el('err').textContent = '删除失败：' + e.message; });
    };

    li.appendChild(left);
    li.appendChild(btn);
    box.appendChild(li);
  });
}

function tick(deadline) {
  if (timer) clearInterval(timer);
  var update = function () {
    var left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    if (left <= 0) {
      el('qrttl').textContent = '这个二维码已过期，重新生成一个。';
      el('qrpane').classList.remove('on');
      if (timer) clearInterval(timer);
      return;
    }
    el('qrttl').textContent = '有效期还剩 ' + left + ' 秒 · 只能用一次';
  };
  update();
  timer = setInterval(update, 1000);
}

el('gen').onclick = function () {
  var btn = el('gen');
  btn.disabled = true;
  el('err').textContent = '';
  fetch('/__mobile/pairing-code', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}'
  })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      el('qrpic').innerHTML = data.qr_svg;
      el('qrurl').textContent = data.pair_url;
      el('qrpane').classList.add('on');
      tick(Date.now() + data.expires_in * 1000);
    })
    .catch(function (e) { el('err').textContent = '生成失败：' + e.message; })
    .then(function () { btn.disabled = false; });
};

loadStatus();
setInterval(loadStatus, 15000);
</script>
</body></html>"""


def render_admin_page() -> str:
    """管理页 HTML。不含任何机器信息：状态与设备列表由页面自己向同源接口取。"""
    return _PAGE


__all__ = ["render_admin_page"]
