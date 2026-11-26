/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* SPMS v1.0.60：自绘「日期 + 整点」选择器。
   - 不再用浏览器原生 datetime-local：原生弹层选完日期后要点小时，双击小时不会收起，
     而且不同浏览器对 step=3600 的分钟显示不一致。
   - 分钟恒为 00；点一下小时立刻写入并收起窗体。
   - 顶部筛选仍写回原来的 date_from/time_from/date_to/time_to 隐藏域，服务端零改动。 */
(function(){
  'use strict';
  function pad(n){ return (n < 10 ? '0' : '') + n; }
  var RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?)?/;
  function norm(v){
    if(v == null) return '';
    var s = String(v).trim();
    if(!s) return '';
    var m = RE.exec(s);
    if(!m) return s;
    var hh = m[4] == null ? 0 : Math.min(23, parseInt(m[4], 10) || 0);
    return m[1] + '-' + m[2] + '-' + m[3] + 'T' + pad(hh) + ':00';
  }
  function disp(v){
    var n = norm(v);
    return n ? n.replace('T', ' ') : '';
  }
  function t(k){ return window.TR ? window.TR(k) : k; }
  window.spmsNormDateTime = norm;
  window.spmsDtDisplay = disp;

  function syncHidden(input){
    var v = norm(input.value), form = input.form;
    if(!form) return;
    var d = form.elements[input.getAttribute('data-date') || ''];
    var tm = form.elements[input.getAttribute('data-time') || ''];
    if(d) d.value = v ? v.slice(0, 10) : '';
    if(tm) tm.value = v ? v.slice(11, 16) : '';
  }

  var panel = null, owner = null, viewY = 0, viewM = 0, selDate = '';
  function close(){
    if(panel && panel.parentNode) panel.parentNode.removeChild(panel);
    panel = null; owner = null;
  }
  function today(){
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function commit(date, hour){
    var val = date + 'T' + pad(hour) + ':00';
    if(owner){
      owner.value = disp(val);
      syncHidden(owner);
      try { owner.dispatchEvent(new Event('change', {bubbles: true})); }
      catch(e){ var ev = document.createEvent('HTMLEvents'); ev.initEvent('change', true, true); owner.dispatchEvent(ev); }
    }
    close();
  }
  function paint(){
    if(!panel) return;
    panel.querySelector('.dtp-title').textContent = viewY + '-' + pad(viewM + 1);
    var first = new Date(viewY, viewM, 1).getDay();
    var dim = new Date(viewY, viewM + 1, 0).getDate();
    var grid = panel.querySelector('.dtp-days');
    grid.innerHTML = '';
    for(var i = 0; i < first; i++){
      var sp = document.createElement('span'); sp.className = 'dtp-blank'; grid.appendChild(sp);
    }
    for(var d = 1; d <= dim; d++){
      var key = viewY + '-' + pad(viewM + 1) + '-' + pad(d);
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'dtp-day' + (key === selDate ? ' is-sel' : '');
      b.setAttribute('data-a', 'day'); b.setAttribute('data-d', key);
      b.textContent = d;
      grid.appendChild(b);
    }
    var hours = panel.querySelectorAll('.dtp-hour');
    var curH = owner ? norm(owner.value).slice(11, 13) : '';
    for(var j = 0; j < hours.length; j++){
      hours[j].className = 'dtp-hour' + (pad(j) === curH ? ' is-sel' : '');
    }
  }
  function build(){
    var el = document.createElement('div');
    el.className = 'dtp-panel';
    var head = document.createElement('div'); head.className = 'dtp-head';
    [['py','‹‹'],['pm','‹'],['title',''],['nm','›'],['ny','››']].forEach(function(x){
      if(x[0] === 'title'){
        var s = document.createElement('span'); s.className = 'dtp-title'; head.appendChild(s); return;
      }
      var b = document.createElement('button'); b.type = 'button'; b.className = 'dtp-nav';
      b.setAttribute('data-a', x[0]); b.textContent = x[1]; head.appendChild(b);
    });
    var wk = document.createElement('div'); wk.className = 'dtp-week';
    ['日','一','二','三','四','五','六'].forEach(function(k){
      var s = document.createElement('span'); s.textContent = t(k); wk.appendChild(s);
    });
    var days = document.createElement('div'); days.className = 'dtp-days';
    var hint = document.createElement('div'); hint.className = 'dtp-hint'; hint.textContent = TR('时间');
    var hs = document.createElement('div'); hs.className = 'dtp-hours';
    for(var h = 0; h < 24; h++){
      var hb = document.createElement('button'); hb.type = 'button'; hb.className = 'dtp-hour';
      hb.setAttribute('data-a', 'hour'); hb.setAttribute('data-h', h); hb.textContent = pad(h) + ':00';
      hs.appendChild(hb);
    }
    var foot = document.createElement('div'); foot.className = 'dtp-foot';
    [['now', TR('现在')],['clear', TR('清除')],['close', TR('关闭')]].forEach(function(x){
      var b = document.createElement('button'); b.type = 'button'; b.setAttribute('data-a', x[0]); b.textContent = x[1];
      foot.appendChild(b);
    });
    el.appendChild(head); el.appendChild(wk); el.appendChild(days);
    el.appendChild(hint); el.appendChild(hs); el.appendChild(foot);
    el.addEventListener('click', function(ev){
      var b = ev.target.closest ? ev.target.closest('button') : null;
      if(!b || !el.contains(b)) return;
      ev.preventDefault(); ev.stopPropagation();
      var a = b.getAttribute('data-a');
      if(a === 'py'){ viewY--; paint(); }
      else if(a === 'ny'){ viewY++; paint(); }
      else if(a === 'pm'){ viewM--; if(viewM < 0){ viewM = 11; viewY--; } paint(); }
      else if(a === 'nm'){ viewM++; if(viewM > 11){ viewM = 0; viewY++; } paint(); }
      else if(a === 'day'){ selDate = b.getAttribute('data-d'); paint(); }
      else if(a === 'hour'){ commit(selDate || today(), parseInt(b.getAttribute('data-h'), 10)); }
      else if(a === 'now'){ var n = new Date(); commit(today(), n.getHours()); }
      else if(a === 'clear'){
        if(owner){ owner.value = ''; syncHidden(owner); owner.dispatchEvent(new Event('change', {bubbles: true})); }
        close();
      }
      else if(a === 'close'){ close(); }
    });
    return el;
  }
  function place(input){
    var r = input.getBoundingClientRect(), w = 292, h = panel.offsetHeight || 340;
    var left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    var top = r.bottom + 4;
    if(top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    panel.style.left = left + 'px';
    panel.style.top = top + 'px';
  }
  function open(input){
    close();
    owner = input;
    var v = norm(input.value);
    if(v){ viewY = parseInt(v.slice(0, 4), 10); viewM = parseInt(v.slice(5, 7), 10) - 1; selDate = v.slice(0, 10); }
    else { var n = new Date(); viewY = n.getFullYear(); viewM = n.getMonth(); selDate = ''; }
    panel = build();
    paint();
    document.body.appendChild(panel);
    place(input);
  }
  document.addEventListener('click', function(ev){
    if(panel && panel.contains(ev.target)) return;
    var el = ev.target.closest ? ev.target.closest('[data-dtp]') : null;
    if(el){ ev.preventDefault(); if(owner === el) close(); else open(el); return; }
    if(panel) close();
  });
  document.addEventListener('keydown', function(ev){ if(ev.key === 'Escape' && panel) close(); });
  window.spmsBindDateTime = function(root){
    (root || document).querySelectorAll('[data-dtp]').forEach(function(el){
      if(el.value) el.value = disp(el.value);
      if(!el.getAttribute('placeholder')) el.setAttribute('placeholder', TR('选择日期时间'));
      if(el.hasAttribute('data-dt-filter')){
        syncHidden(el);
        if(!el.getAttribute('data-dt-bound')){
          el.setAttribute('data-dt-bound', '1');
          el.addEventListener('change', function(){
            el.value = disp(el.value);   // 外部脚本/粘贴进来的分钟一律归零
            syncHidden(el);
          });
        }
      }
    });
  };
  document.addEventListener('DOMContentLoaded', function(){ window.spmsBindDateTime(document); });
})();
