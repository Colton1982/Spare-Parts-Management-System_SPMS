/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* v1.0.20 维修品类型可搜索下拉
 * ---------------------------------------------------------------
 * 旧实现：repairOpts() 在**每一行**维修记录里输出一整份 <optgroup> 全量字典。
 *   3000 词条 × 20 行 = 6 万个 <option>，一次重绘要 1~3 秒，输入框直接失焦。
 * 新实现：所有行共用一个浮层面板，聚焦时才渲染「命中前 50 条」，
 *   输入即过滤（词条名 / 一级类目名都能搜），点选写回 input.value 并触发 change，
 *   提交 payload 格式与旧版完全一致（仍是 repair_type 一个字符串），后端零改动。
 */
(function () {
  'use strict';
  var MAX_ITEMS = 50;
  var pop = null;
  var cur = null;

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  // 取名 TR 而不是 tr：i18n 扫描器只认 TR/TRN/TRP/anTitle 这四种取词写法，
  // 用小写会被当成「裸写中文」报出来（check_v319/320/321 三道门禁都卡这一条）。
  function TR(s) {
    return (typeof window.TR === 'function') ? window.TR(s) : s;
  }

  function ensurePop() {
    if (pop) return pop;
    pop = document.createElement('div');
    pop.className = 'rp-pop';
    pop.style.display = 'none';
    document.body.appendChild(pop);
    // mousedown 而不是 click：click 会让 input 先失焦，change 顺序乱掉
    pop.addEventListener('mousedown', function (e) {
      var it = e.target.closest ? e.target.closest('.rp-item') : null;
      if (!it || !cur) return;
      e.preventDefault();
      apply(it.getAttribute('data-v') || '');
    });
    return pop;
  }

  function apply(v) {
    if (!cur) return;
    var el = cur;
    el.value = v;
    try {
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } catch (err) {
      var ev = document.createEvent('HTMLEvents');
      ev.initEvent('change', true, false);
      el.dispatchEvent(ev);
    }
    hide();
  }

  function hide() {
    if (pop) pop.style.display = 'none';
    cur = null;
  }

  function render(kw) {
    var cats = window.REPAIR_CATS || [];
    var q = (kw || '').trim().toLowerCase();
    var groups = [];
    var total = 0;
    for (var i = 0; i < cats.length; i++) {
      var g = cats[i] || {};
      var items = g.items || [];
      var hit = [];
      for (var j = 0; j < items.length; j++) {
        var c = items[j];
        if (!q
          || String(c).toLowerCase().indexOf(q) >= 0
          || String(g.group || '').toLowerCase().indexOf(q) >= 0) {
          hit.push(c);
        }
      }
      if (hit.length) {
        groups.push({ group: g.group || '', items: hit });
        total += hit.length;
      }
    }
    var h = '';
    var shown = 0;
    for (var a = 0; a < groups.length && shown < MAX_ITEMS; a++) {
      h += '<div class="rp-group">' + esc(groups[a].group) + '</div>';
      for (var b = 0; b < groups[a].items.length && shown < MAX_ITEMS; b++) {
        var v = groups[a].items[b];
        h += '<div class="rp-item" data-v="' + esc(v) + '">' + esc(v) + '</div>';
        shown++;
      }
    }
    if (!shown) {
      h = '<div class="rp-empty">' + esc(TR('无匹配项，可直接输入新类型')) + '</div>';
    } else if (total > shown) {
      h += '<div class="rp-more">' + esc(TR('共 {n} 条，继续输入缩小范围').replace('{n}', total - shown)) + '</div>';
    }
    ensurePop();
    pop.innerHTML = h;
    pop.style.display = 'block';
    var r = cur.getBoundingClientRect();
    var hgt = Math.min(260, pop.scrollHeight || 260);
    var top = r.bottom + 4;
    if (top + hgt > window.innerHeight - 8) top = Math.max(8, r.top - hgt - 4);
    pop.style.left = Math.max(8, r.left) + 'px';
    pop.style.top = top + 'px';
    pop.style.minWidth = Math.max(170, r.width) + 'px';
  }

  document.addEventListener('focusin', function (e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('rp-input')) return;
    cur = t;
    render(t.value || '');
  });

  document.addEventListener('input', function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('rp-input') && cur === t) render(t.value);
  });

  document.addEventListener('mousedown', function (e) {
    if (!pop || pop.style.display === 'none') return;
    var t = e.target;
    if (pop.contains(t)) return;
    if (t && t.classList && t.classList.contains('rp-input')) return;
    hide();
  });

  document.addEventListener('keydown', function (e) {
    if (!cur) return;
    if (e.key === 'Escape') { hide(); return; }
    if (e.key === 'Enter') {
      e.preventDefault();
      var first = pop && pop.querySelector('.rp-item');
      if (first) apply(first.getAttribute('data-v') || '');
    }
  });

  window.addEventListener('resize', hide);
  window.addEventListener('scroll', function () { if (cur) hide(); }, true);

  /** 生成一行的维修品类型输入框（替代旧的全量 <select>）。
   *  idx: 行号；val: 当前值；setter: 行数据写回函数名（默认 setRow，出入库页是 setIoRow）。 */
  window.repairPick = function (idx, val, setter) {
    var v = esc(val || '');
    var fn = setter || 'setRow';
    return '<input class="f-input rp-input" type="text" autocomplete="off"'
      + ' style="max-width:150px"'
      + ' placeholder="' + esc(TR('维修品类型…')) + '"'
      + ' value="' + v + '"'
      + ' onchange="' + fn + '(' + idx + ",'repair_type',this.value)\">";
  };
})();
