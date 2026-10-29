// Real FastAPI/Graph/SQLite with a deterministic model. Delayed responses and HTTP 503 are deliberate fault injection.
/* global process */
import { test, expect } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { URL } from 'node:url';
const proof=process.env.CHAIAI_SCENE_PROOF_DIR || resolve(import.meta.dirname,'../test-results/question-queue-proof');
mkdirSync(proof,{recursive:true});
const draft=page=>page.getByRole('textbox',{name:'给智能体发消息'});
const queue=page=>page.getByRole('region',{name:'待发送的消息'});
async function start(page){await page.goto('/');await page.getByRole('button',{name:'新建会话',exact:true}).click();await expect(page.locator('[data-empty-conversation="true"]')).toBeVisible();await expect(page.getByRole('button',{name:'选择会话模型'})).not.toHaveText('选择模型');}
async function send(page,text){await draft(page).fill(text);await draft(page).press('Enter');}
function watch(page){const posted=[];page.on('request',r=>{if(r.method()==='POST'&&/\/api\/sessions\/[^/]+\/messages$/.test(r.url()))posted.push(r.postDataJSON());});return posted;}
async function hold(page,name='QUEUE_START'){
 let release;const gate=new Promise(r=>release=r);let ready;const arrived=new Promise(r=>ready=r);
 await page.route('**/api/sessions/*/messages',async route=>{
  if(route.request().postDataJSON().text!==name)return route.fallback();
  const response=await route.fetch();ready();await gate;
  try{await route.fulfill({response});}catch{ /* A reload can close the original request. */ }
 });
 await send(page,name);await arrived;return release;
}
async function question(page,name){await start(page);await send(page,name);const card=page.getByRole('region',{name:'提问',exact:true});await expect(card).toBeVisible();return card;}

test('队列可上下移动、删除，并按修改后的顺序真正发送；可编辑内容',async({page})=>{
 const timeline=[];const diagnostics=[];
 page.on('response',response=>{
  if(response.request().method()!=='POST'||!response.url().endsWith('/messages'))return;
  const capture=(async()=>{
   const body=await response.json();const item={text:response.request().postDataJSON().text,http:response.status(),status:body.status,error:body.error?.code};
   if(response.status()===409){const tasks=await (await page.request.get('/api/tasks')).json();item.tasks=tasks.tasks.filter(t=>t.session_title==='QUEUE_START').map(t=>({session:t.session_id,state:t.state}));}
   timeline.push(item);
  })();diagnostics.push(capture.catch(()=>{}));
 });
 await start(page);const posted=watch(page);const release=await hold(page);
 try{
  for(const text of ['队列 A','队列 B','队列 C'])await send(page,text);
  const q=queue(page);await expect(q.getByRole('listitem')).toHaveCount(3);
  await expect(q.getByRole('button',{name:'上移第 1 条'})).toBeDisabled();
  await expect(q.getByRole('button',{name:'下移第 3 条'})).toBeDisabled();
  await q.getByRole('button',{name:'删除第 2 条'}).click();
  await q.getByRole('button',{name:'上移第 2 条'}).click();
  await expect(q.getByRole('listitem').first()).toContainText('队列 C');
  await q.getByRole('button',{name:'下移第 1 条'}).click();
  await expect(q.getByRole('listitem').first()).toContainText('队列 A');
  await q.getByRole('button',{name:'上移第 2 条'}).click();
  await q.getByRole('button',{name:'编辑第 1 条'}).click();
  const editor=page.getByRole('dialog',{name:'编辑待发消息'});
  await editor.getByRole('textbox',{name:'待发消息内容'}).fill('队列 C 已修改');
  await editor.getByRole('button',{name:'保存修改'}).click();
  await expect(q.getByRole('listitem').first()).toContainText('队列 C 已修改');
  await page.screenshot({path:proof+'/user-scenes-queue.png'});
  release();await expect(q).toHaveCount(0);
  await expect.poll(()=>posted.map(p=>p.text)).toEqual(['QUEUE_START','队列 C 已修改','队列 A']);
  expect(new Set(posted.map(p=>p.client_request_id)).size).toBe(3);
 }finally{release();await Promise.all(diagnostics);await test.info().attach('queue-request-timeline',{body:JSON.stringify(timeline,null,2),contentType:'application/json'});writeFileSync(proof+'/user-scenes-queue-timeline.json',JSON.stringify(timeline,null,2));}
});

test('队列达到 20 条后保留被拒绝的草稿；刷新清空队列',async({page})=>{
 await start(page);const posted=watch(page);const release=await hold(page,'QUEUE_LIMIT');
 try{
  for(let i=1;i<=20;i++)await send(page,'待发 '+i);
  await expect(queue(page).getByRole('listitem')).toHaveCount(20);
  await send(page,'第 21 条草稿');await expect(page.getByText(/待发队列已满/)).toBeVisible();
  await expect(draft(page)).toHaveValue('第 21 条草稿');
  await page.reload();await expect(draft(page)).toBeVisible();await expect(queue(page)).toHaveCount(0);
  expect(posted.map(p=>p.text)).toEqual(['QUEUE_LIMIT']);
 }finally{release();}
});

test('自动发送失败时暂停保留；继续发送复用请求标识',async({page})=>{
 await start(page);const posted=watch(page);let failed=false;
 await page.route('**/api/sessions/*/messages',route=>{
  if(route.request().postDataJSON().text==='失败后重试'&&!failed){failed=true;return route.fulfill({status:503,json:{detail:'场景测试：暂时不可用'}});}
  return route.fallback();
 });
 const release=await hold(page,'QUEUE_RETRY');await send(page,'失败后重试');release();
 await expect(queue(page)).toContainText('已暂停');await expect(queue(page)).toContainText('失败后重试');
 await page.getByRole('button',{name:'继续发送',exact:true}).click();await expect(queue(page)).toHaveCount(0);
 await expect.poll(()=>posted.filter(p=>p.text==='失败后重试').length).toBe(2);
 const retry=posted.filter(p=>p.text==='失败后重试');expect(retry[0].client_request_id).toBe(retry[1].client_request_id);
});

test('题组翻页和刷新保留答案；一次提交整组并继续原轮次',async({page})=>{
 const card=await question(page,'ASK_GROUP');let path;const payloads=[];
 await page.route('**/api/sessions/*/questions/*',route=>{path=new URL(route.request().url()).pathname;payloads.push(route.request().postDataJSON());return route.continue();});
 await card.getByRole('button',{name:'测试环境',exact:true}).click();await card.getByRole('button',{name:'下一题'}).click();
 await card.getByRole('textbox',{name:'第 2 题的回答'}).fill('feature/release');
 await card.getByRole('button',{name:'上一题'}).click();await expect(card.getByRole('textbox',{name:'第 1 题的回答'})).toHaveValue('测试环境');
 await page.reload();await expect(card.getByRole('textbox',{name:'第 1 题的回答'})).toHaveValue('测试环境');
 await card.getByRole('button',{name:'下一题'}).click();await expect(card.getByRole('textbox',{name:'第 2 题的回答'})).toHaveValue('feature/release');
 await card.getByRole('button',{name:'下一题'}).click();await expect(card.getByRole('textbox')).toHaveCount(0);
 await card.getByRole('button',{name:'要',exact:true}).click();await page.screenshot({path:proof+'/user-scenes-question.png'});
 await card.getByRole('button',{name:'提交',exact:true}).click();await expect(card).toHaveCount(0);await expect(draft(page)).toBeVisible();
 expect(payloads).toHaveLength(1);expect(payloads[0].answers.map(a=>a.answer)).toEqual(['测试环境','feature/release','要']);
 const snap=await (await page.request.get(path.split('/questions/')[0])).json();expect(snap.question.state).toBe('answered');expect(snap.question.items.map(x=>x.answer)).toEqual(['测试环境','feature/release','要']);
});

test('填过一题后全部跳过，后端不保留已输入答案',async({page})=>{
 const card=await question(page,'ASK_SKIP');await card.getByRole('textbox',{name:'第 1 题的回答'}).fill('这段不要提交');
 const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().includes('/questions/'));
 await card.getByRole('button',{name:'全部跳过'}).click();const result=await response;expect(result.ok()).toBe(true);
 await expect(card).toHaveCount(0);const path=new URL(result.url()).pathname.split('/questions/')[0];
 const snap=await (await page.request.get(path)).json();expect(snap.question.state).toBe('skipped');expect(snap.question.items.every(x=>x.answer===null&&x.skipped)).toBe(true);
});

test('答复提交失败后刷新，应该保留尚未被后端接受的答案',async({page})=>{
 const card=await question(page,'ASK_RETRY');
 await page.route('**/api/sessions/*/questions/*',route=>route.fulfill({status:503,json:{detail:'场景测试：提交失败'}}));
 await card.getByRole('textbox',{name:'第 1 题的回答'}).fill('刷新后还要保留');
 await card.getByRole('button',{name:'下一题'}).click();await card.getByRole('button',{name:'下一题'}).click();
 await card.getByRole('button',{name:'提交',exact:true}).click();await expect(card.getByRole('button',{name:'提交',exact:true})).toBeEnabled();
 await page.reload();await expect(card.getByRole('textbox',{name:'第 1 题的回答'})).toHaveValue('刷新后还要保留');
});

test('编辑期间本轮结束也不抢发；空内容不能保存，取消和保存按预期工作',async({page})=>{
 await start(page);const posted=watch(page);const release=await hold(page,'EDIT_RUNNING');
 await send(page,'原始待发消息');await queue(page).getByRole('button',{name:'编辑第 1 条'}).click();
 const editor=page.getByRole('dialog',{name:'编辑待发消息'});
 release();await expect(editor).toBeVisible();
 await editor.getByRole('textbox',{name:'待发消息内容'}).fill('');await expect(editor.getByRole('button',{name:'保存修改'})).toBeDisabled();
 await editor.getByRole('textbox',{name:'待发消息内容'}).fill('编辑窗口中的消息');
 expect(posted.map(x=>x.text)).toEqual(['EDIT_RUNNING']);
 await editor.getByRole('button',{name:'保存修改'}).click();
 await expect.poll(()=>posted.map(x=>x.text)).toEqual(['EDIT_RUNNING','编辑窗口中的消息']);
});

test('手机窗口可以编辑、取消并删除队列消息',async({page})=>{
 await page.setViewportSize({width:390,height:844});await start(page);const release=await hold(page,'MOBILE_QUEUE');
 try{
  await send(page,'手机上的第一条');await send(page,'手机上的第二条');
  await queue(page).getByRole('button',{name:'编辑第 2 条'}).click();
  const editor=page.getByRole('dialog',{name:'编辑待发消息'});await editor.getByRole('textbox',{name:'待发消息内容'}).fill('取消不应保存');
  const box=await editor.boundingBox();expect(box.x).toBeGreaterThanOrEqual(0);expect(box.x+box.width).toBeLessThanOrEqual(390);
  await page.screenshot({path:proof+'/user-scenes-queue-edit-mobile.png'});
  await editor.getByRole('button',{name:'取消',exact:true}).click();await expect(queue(page)).toContainText('手机上的第二条');
  await queue(page).getByRole('button',{name:'删除第 2 条'}).click();await queue(page).getByRole('button',{name:'删除第 1 条'}).click();
  await expect(queue(page)).toHaveCount(0);
 }finally{release();}
});

test('停止后队列暂停；点击继续后等待原 worker 退出再发送',async({page})=>{
 await start(page);const posted=watch(page);await send(page,'STOP_SCENE');
 await expect(page.getByRole('button',{name:'停止生成',exact:true})).toBeVisible();
 await send(page,'停止后的下一条');
 await page.getByRole('button',{name:'停止生成',exact:true}).click();
 await expect(queue(page)).toContainText('已暂停');
 expect(posted.map(x=>x.text)).toEqual(['STOP_SCENE']);
 await page.getByRole('button',{name:'继续发送',exact:true}).click();
 await expect(queue(page)).toHaveCount(0,{timeout:20000});
 await expect.poll(()=>posted.some(x=>x.text==='停止后的下一条')).toBe(true);
});

test('A 自动发送中切到 B，A 失败只恢复 A；回到 A 可继续发送',async({page})=>{
 await start(page);const release=await hold(page,'SESSION_A');let fail;let arrived;
 const pending=new Promise(r=>fail=r);const reached=new Promise(r=>arrived=r);let failed=false;
 await page.route('**/api/sessions/*/messages',async route=>{
  if(route.request().postDataJSON().text!=='A 的自动消息'||failed)return route.fallback();
  failed=true;arrived();await pending;await route.fulfill({status:503,json:{detail:'A 暂时断网'}});
 });
 await send(page,'A 的自动消息');release();await reached;
 await page.getByRole('button',{name:'新建会话',exact:true}).click();await expect(page.locator('[data-empty-conversation="true"]')).toBeVisible();
 await expect(queue(page)).toHaveCount(0);await send(page,'B 的独立消息');
 await expect(page.getByRole('log',{name:'对话'})).toContainText('B 的独立消息');fail();
 await page.getByRole('navigation',{name:'会话',exact:true}).getByText('SESSION_A',{exact:true}).first().click();
 await expect(queue(page)).toContainText('已暂停');await expect(queue(page)).toContainText('A 的自动消息');
 await page.getByRole('button',{name:'继续发送',exact:true}).click();await expect(queue(page)).toHaveCount(0);
});

test('已经开始生成后停止，继续队列也不会重复执行下一条',async({page})=>{
 await start(page);await send(page,'STOP_SCENE');
 await expect(page.getByRole('log',{name:'对话'}).getByText('STOP_SCENE',{exact:true})).toBeVisible();
 await expect(page.getByRole('button',{name:'停止生成',exact:true})).toBeVisible();
 await send(page,'运行中停止后的消息');await page.getByRole('button',{name:'停止生成',exact:true}).click();
 await expect(queue(page)).toContainText('已暂停');await page.getByRole('button',{name:'继续发送',exact:true}).click();
 await expect(queue(page)).toHaveCount(0,{timeout:20000});
 await expect(page.getByRole('log',{name:'对话'}).getByText('运行中停止后的消息',{exact:true})).toHaveCount(1,{timeout:20000});
});
