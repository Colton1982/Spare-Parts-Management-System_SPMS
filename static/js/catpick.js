/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* 分类选择器（配 templates/_catpick.html 的 cat_picker 宏使用）
 *
 * 为什么不用原生 <select>：弹出层高度由浏览器决定，分类一多就铺满半屏、
 * 还会盖住后面的表单字段。这里换成自绘面板：
 *   - 列表区限高 200px，超出滚动
 *   - 带搜索框，分类多也能直接找
 *   - 面板改为 fixed 定位，避免被弹窗的 overflow 裁掉
 *   - 空间不够自动朝上展开
 * 值仍提交在 <input type="hidden" name="category"> 里，表单逻辑不用改。
 */
var lastOpenAt = 0;

function catCloseAll() {
  document.querySelectorAll('.cat-pop').forEach(function (p) { p.hidden = true; });
}

function catOpen(btn) {
  var wrap = btn.closest('.cat-pick');
  var pop = wrap.querySelector('.cat-pop');
  var wasOpen = !pop.hidden;
  catCloseAll();
  if (wasOpen) return;

  lastOpenAt = Date.now();
  var r = btn.getBoundingClientRect();
  var list = pop.querySelector('.cat-opts');
  pop.hidden = false;
  pop.style.position = 'fixed';
  pop.style.right = 'auto';
  pop.style.left = Math.round(r.left) + 'px';
  pop.style.width = Math.round(r.width) + 'px';
  pop.style.top = '0px';

  var panelH = pop.offsetHeight;
  var below = window.innerHeight - r.bottom - 12;
  var above = r.top - 12;
  var up = (below < Math.min(panelH, 240)) && (above > below);
  list.style.maxHeight = Math.max(120, Math.min(200, (up ? above : below) - 44)) + 'px';
  pop.style.top = Math.round(up ? Math.max(8, r.top - pop.offsetHeight - 4) : r.bottom + 4) + 'px';

  var q = pop.querySelector('.cat-q');
  if (q) { q.value = ''; catFilter(q); q.focus(); }
  var on = pop.querySelector('.cat-opt.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
}

function catFilter(inp) {
  var pop = inp.closest('.cat-pop');
  var q = (inp.value || '').trim().toLowerCase();
  var n = 0;
  pop.querySelectorAll('.cat-opt').forEach(function (o) {
    var hit = !q || (o.dataset.v || '').toLowerCase().indexOf(q) >= 0;
    o.hidden = !hit;
    if (hit) n++;
  });
  var none = pop.querySelector('.cat-none');
  if (none) none.hidden = n > 0;
}

function catPick(opt) {
  var wrap = opt.closest('.cat-pick');
  var hid = wrap.querySelector('input[type=hidden]');
  hid.value = opt.dataset.v || '';
  wrap.querySelector('.cat-txt').textContent = hid.value || wrap.dataset.empty || '';
  wrap.querySelector('.cat-btn').title = hid.value || wrap.dataset.empty || '';
  wrap.querySelectorAll('.cat-opt').forEach(function (o) { o.classList.toggle('on', o === opt); });
  wrap.querySelector('.cat-pop').hidden = true;
  hid.dispatchEvent(new Event('change', { bubbles: true }));
}

/* 编辑弹窗回填：老的分类若已从字典里删掉，面板里没有这一项 → 现场补一条，
   否则赋值不上，一保存分类就丢了。 */
function setCatPicker(id, val) {
  var hid = document.getElementById(id);
  if (!hid) return;
  var wrap = hid.closest('.cat-pick');
  val = val || '';
  var hit = null;
  wrap.querySelectorAll('.cat-opt').forEach(function (o) {
    if ((o.dataset.v || '') === val) hit = o;
    o.classList.toggle('on', (o.dataset.v || '') === val);
  });
  if (val && !hit) {
    hit = document.createElement('div');
    hit.className = 'cat-opt on';
    hit.dataset.v = val;
    hit.title = val;
    hit.textContent = val;
    hit.onclick = function () { catPick(hit); };
    wrap.querySelector('.cat-opts').insertBefore(hit, wrap.querySelector('.cat-none'));
  }
  hid.value = val;
  wrap.querySelector('.cat-txt').textContent = val || wrap.dataset.empty || '';
  wrap.querySelector('.cat-btn').title = val || wrap.dataset.empty || '';
}

document.addEventListener('click', function (e) {
  if (!e.target.closest('.cat-pick')) catCloseAll();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') catCloseAll();
});
/* 弹窗内部滚动会让 fixed 面板错位，滚动 / 改窗口大小直接收起。
   但打开瞬间会 scrollIntoView 把选中项滚进视野，那次滚动的 target 在面板内部，
   不能当成「页面滚动」处理，否则面板刚打开就被自己关掉。 */
window.addEventListener('scroll', function (e) {
  var t = e.target;
  if (t && t.closest && t.closest('.cat-pop')) return;
  if (Date.now() - lastOpenAt < 250) return;
  catCloseAll();
}, true);
window.addEventListener('resize', catCloseAll);
