/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* 全局小工具脚本 */
(function(){
  /* 提示条的去留统一由 base.html 的 smtAutoClose 决定（成功自己走、报错常驻）。
     这里以前还挂了一段「4 秒无条件 remove 所有 .flash」，它和 base.html 抢着删，
     于是错误提示照样 4 秒消失 —— 用户反馈的「报错还没看清就没了」就有它一半责任。 */

  function esc(s){ var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  // v1.0.71：调库源下拉排序——同库位聚拢、批次从早到晚、待维修/待报废库位沉底。
  // 库位属性以 locations.attribute 为准；没有日期的批次（B-LEGACY）视为最早排前。
  function locAttrRank(l){
    var a = l && l.attribute ? String(l.attribute) : '';
    if(a === 'pending_repair' || a === 'pending_scrap' ||
       String(l && l.condition || '') === 'defective') return 2;
    return 1;
  }
  window.sortTransferLocs = function(locs){
    return (locs || []).slice().sort(function(a, b){
      var ra = locAttrRank(a), rb = locAttrRank(b);
      if(ra !== rb) return ra - rb;                              // 待维修/待报废/不良沉底
      var la = String(a.location || '\uffff'), lb = String(b.location || '\uffff');
      if(la !== lb) return la < lb ? -1 : 1;                     // 相同库位聚一起
      var da = String(a.in_date || ''), db = String(b.in_date || '');
      if(da !== db) return da < db ? -1 : 1;                     // 批次从早到晚
      var ba = String(a.batch_no || ''), bb = String(b.batch_no || '');
      if(ba !== bb) return ba < bb ? -1 : 1;
      return (Number(a.inv_id) || 0) - (Number(b.inv_id) || 0);  // 兜底稳定
    });
  };

  // v1.0.73：库位下拉按「库位」合并，不按批次重复。
  // onlySpecial=true：只合并待维修/待报废（这些是共用库位，批次无意义），
  // 良品行仍逐批次保留——批次级调库要能选到具体 inv_id，语义不能丢。
  // onlySpecial=false：全部按 location_id 合并（目标库位只要库位，不要批次）。
  window.mergeLocsByLocation = function(locs, onlySpecial){
    var out = [], idx = {};
    (locs || []).forEach(function(l){
      var attr = l && l.attribute ? String(l.attribute) : 'good';
      var special = attr !== 'good' || String(l && l.condition || '') === 'defective';
      var merge = onlySpecial ? special : true;
      var key = merge ? ('loc-' + String(l.location_id == null ? (l.location || 'null') : l.location_id))
                      : ('row-' + String(l.inv_id != null ? l.inv_id :
                          (String(l.location_id) + '-' + String(l.batch_no || '') + '-' + String(l.condition || ''))));
      var prev = idx[key];
      if(!prev){
        var copy = {};
        for(var k in l){ if(Object.prototype.hasOwnProperty.call(l, k)) copy[k] = l[k]; }
        if(merge) copy.batch_no = '';
        idx[key] = copy;
        out.push(copy);
        return;
      }
      prev.quantity = (Number(prev.quantity) || 0) + (Number(l.quantity) || 0);
      if(l.repair_qty != null || String(l.condition || '') === 'repair'){
        var rep = l.repair_qty != null ? (Number(l.repair_qty) || 0) : (Number(l.quantity) || 0);
        prev.repair_qty = (Number(prev.repair_qty) || 0) + rep;
        prev.condition = 'repair';
      }
      if(l.in_date && (!prev.in_date || String(l.in_date) < String(prev.in_date))) prev.in_date = l.in_date;
    });
    return out;
  };

  // v1.0.78：调库源下拉「库位 → 批次」两级展示。
  // 每组第一项是「整库位」（value=locall_<库位id>），选中后自动填整库数量并锁数量框；
  // 组内其余是批次行（value=inv_<库存行id>），选中后自动填该批数量、允许改小。
  // locs 为 stock_locations() 下发；fmtFn 负责数量格式化；sel 是当前 value。
  window.transferSourceOptions = function(locs, sel, fmtFn, wholeLabel){
    var groups = [], idx = {};
    window.sortTransferLocs(locs).forEach(function(l){
      var raw = l && l.location_id != null ? String(l.location_id) : 'NULL';
      if(!idx[raw]){
        var g = {key:raw, name:String(l.location || ''), rows:[], cond:String(l && l.condition || 'good')};
        idx[raw] = g; groups.push(g);
      }
      idx[raw].rows.push(l);
    });
    groups.sort(function(a, b){
      var ra = a.rows[0] ? locAttrRank(a.rows[0]) : 1;
      var rb = b.rows[0] ? locAttrRank(b.rows[0]) : 1;
      if(ra !== rb) return ra - rb;
      return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0);
    });
    var fmt = fmtFn || function(v){ return v == null ? '' : String(v); };
    var selS = String(sel || '');
    var out = ['<option value="">' + TR('源库位…') + '</option>'];
    groups.forEach(function(g){
      var total = 0;
      g.rows.forEach(function(l){ total += Number(l.quantity) || 0; });
      var gl = g.name + (g.key === 'NULL'
        ? locParen(COND_NAMES && COND_NAMES[g.cond] ? COND_NAMES[g.cond] : '')
        : '');
      out.push('<optgroup label="' + esc(gl + TR('（') + g.rows.length + TR('批次') + TR('）')) + '">');
      var allKey = 'locall_' + g.key;
      out.push('<option value="' + allKey + '"' + (selS === allKey ? ' selected' : '') + '>' +
        (wholeLabel || TR('整库位')) + locParen(TR('数量') + ' ' + fmt(total)) + '</option>');
      g.rows.forEach(function(l){
        var v = 'inv_' + l.inv_id;
        var bat = String(l.batch_no || '').trim() ? ' · ' + TR('批次') + ' ' + esc(String(l.batch_no).trim()) : '';
        var cdt = (l.condition && l.condition !== 'good' && COND_NAMES && COND_NAMES[l.condition])
          ? ' · ' + esc(COND_NAMES[l.condition]) : '';
        out.push('<option value="' + v + '"' + (selS === v ? ' selected' : '') + '>' +
          esc(g.name) + locParen(TR('数量') + ' ' + fmt(l.quantity) + bat + cdt) + '</option>');
      });
      out.push('</optgroup>');
    });
    if(!groups.length) out.push('<option value="" disabled>' + TR('该物料无任何库存') + '</option>');
    return out;
  };

  // v1.0.78：调库源 value（inv_x / locall_x / 老 loc_x）→ 数量与状态。
  // 提交前端自动填数、后端校验都走这一个口径，避免两页各写一份漂移。
  window.transferSourceInfo = function(locs, key){
    var k = String(key || ''), rows = locs || [];
    if(k.indexOf('locall_') === 0){
      var lid = k.slice(7), total = 0, cond = 'good', n = 0;
      rows.forEach(function(l){
        var raw = l.location_id == null ? 'NULL' : String(l.location_id);
        if(raw !== lid) return;
        total += Number(l.quantity) || 0;
        if(!n) cond = String(l.condition || 'good');
        if(String(l.condition || 'good') !== cond && cond !== 'defective') cond = 'defective';
        n++;
      });
      return {qty: total, cond: cond, rows: n, whole: true};
    }
    if(k.indexOf('inv_') === 0){
      var iid = k.slice(4);
      var hit = rows.find(function(l){ return String(l.inv_id) === iid; });
      return hit ? {qty: Number(hit.quantity) || 0, cond: String(hit.condition || 'good'),
                    rows: 1, whole: false, row: hit} : null;
    }
    if(k.indexOf('loc_') === 0){
      var raw2 = k.slice(4), total2 = 0, cond2 = 'good', n2 = 0;
      rows.forEach(function(l){
        var rr = l.location_id == null ? 'NULL' : String(l.location_id);
        if(rr !== raw2) return;
        total2 += Number(l.quantity) || 0;
        if(!n2) cond2 = String(l.condition || 'good');
        n2++;
      });
      return n2 ? {qty: total2, cond: cond2, rows: n2, whole: false} : null;
    }
    return null;
  };

  // ============ 弹窗分页（单选 / 多选选择器共用） ============
  // 默认每页 10 条，可切 10/20/50/100。总数取响应头 X-Total-Count，
  // 接口 JSON 仍是数组（移动盘点页等旧消费方只读数组，不能改结构）。
  var PK_SIZES = [10, 20, 50, 100];

  function pkPagerBar(prefix){
    var opts = '';
    PK_SIZES.forEach(function(v){
      opts += '<option value="' + v + '">' + TRP('每页 {n} 条', {n: v}) + '</option>';
    });
    return '<div class="pk-pager">' +
        '<select class="f-input ' + prefix + '-size" style="max-width:130px">' + opts + '</select>' +
        '<span class="muted small ' + prefix + '-info"></span>' +
        '<button type="button" class="btn btn-xs ' + prefix + '-first">' + TR('首页') + '</button>' +
        '<button type="button" class="btn btn-xs ' + prefix + '-prev">' + TR('上一页') + '</button>' +
        '<button type="button" class="btn btn-xs ' + prefix + '-next">' + TR('下一页') + '</button>' +
        '<button type="button" class="btn btn-xs ' + prefix + '-last">' + TR('末页') + '</button>' +
      '</div>';
  }

  // 页码信息与按钮可用状态：页数一律由 total/size 现算，不信任残留值
  function pkSync(root, prefix, st){
    var pages = Math.max(1, Math.ceil((st.total || 0) / st.size));
    var info = root.querySelector('.' + prefix + '-info');
    if(info){
      info.textContent = st.total
        ? TRP('共 {total} 条 · 第 {page}/{pages} 页', {total: st.total, page: st.page, pages: pages})
        : '';
    }
    var dis = {first: st.page <= 1, prev: st.page <= 1, next: st.page >= pages, last: st.page >= pages};
    Object.keys(dis).forEach(function(k){
      var b = root.querySelector('.' + prefix + '-' + k);
      if(b) b.disabled = dis[k];
    });
  }

  function pkBind(root, prefix, st, onGo){
    var sel = root.querySelector('.' + prefix + '-size');
    if(sel){
      sel.value = String(st.size);
      sel.addEventListener('change', function(){
        st.size = parseInt(this.value, 10) || 10;
        st.page = 1;          // 换条数回到第一页，否则可能落在空页
        onGo();
      });
    }
    ['first', 'prev', 'next', 'last'].forEach(function(k){
      var b = root.querySelector('.' + prefix + '-' + k);
      if(!b) return;
      b.addEventListener('click', function(){
        var pages = Math.max(1, Math.ceil((st.total || 0) / st.size));
        if(k === 'first') st.page = 1;
        else if(k === 'prev') st.page = Math.max(1, st.page - 1);
        else if(k === 'next') st.page = Math.min(pages, st.page + 1);
        else st.page = pages;
        onGo();
      });
    });
  }

  // ============ 高级备件选择器（出入库申请 / 出入库管理 / 调库管理 共用） ============
  // 用法：
  //   <input type="hidden" name="part_id" id="xxx-part-id">
  //   <button type="button" class="btn" onclick="PartPicker.show('xxx-part-id','xxx-part-name')">🔍 选择物品…</button>
  //   <span id="xxx-part-name" class="muted">未选择</span>
  // 说明：支持按 料号/名称/规格/分类 关键词即时搜索，避免千级备件下拉选择困难。
  var mask = null, curInput = null, curName = null, timer = null, curCallback = null;
  var pst = {kw: '', size: 10, page: 1, total: 0};

  function ensure(){
    if(mask) return;
    mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.style.display = 'none';
    mask.addEventListener('click', function(e){ if(e.target === mask) hide(); });
    mask.innerHTML =
      '<div class="modal">' +
        '<div class="modal-head"><span>' + TR('选择备件') + '（' + TR('支持按') + ' ' + TR('料号') + ' / ' + TR('名称') + ' / ' + TR('规格') + ' ' + TR('搜索') + '）</span>' +
          '<button type="button" class="modal-x" onclick="PartPicker.hide()">✕</button></div>' +
        '<div class="modal-body">' +
          '<div class="inline-form" style="margin-bottom:10px">' +
            '<input class="f-input picker-kw" placeholder="' + TR('输入') + ' ' + TR('料号') + ' / ' + TR('名称') + ' / ' + TR('规格') + ' ' + TR('关键词') + '…" style="flex:1;min-width:220px">' +
            '<button type="button" class="btn btn-primary picker-go">' + TR('搜索') + '</button>' +
            '<span class="muted small">(' + TR('输入即搜') + ')</span>' +
          '</div>' +
          '<div class="table-wrap" style="max-height:52vh;overflow-y:auto">' +
            '<table class="table"><thead><tr><th>' + TR('料号') + '</th><th>' + TR('名称') + '</th><th>' + TR('规格') + '</th><th>' + TR('库存') + '</th><th>' + TR('单价') + '(元)</th><th>' + TR('贵重') + '</th><th>' + TR('操作') + '</th></tr></thead>' +
            '<tbody class="picker-rows"><tr><td colspan="7" class="muted center">' + TR('输入关键词搜索，或直接点击「搜索」列出全部备件') + '</td></tr></tbody>' +
          '</table></div>' +
          pkPagerBar('picker') +
        '</div>' +
      '</div>';
    document.body.appendChild(mask);
    var kw = mask.querySelector('.picker-kw');
    var go = mask.querySelector('.picker-go');
    function doSearch(){ pst.page = 1; search(); }
    kw.addEventListener('input', function(){ clearTimeout(timer); timer = setTimeout(doSearch, 300); });
    kw.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); doSearch(); } });
    go.addEventListener('click', doSearch);
    pkBind(mask, 'picker', pst, function(){ search(); });
  }

  function search(){
    var url = '/api/parts/search?kw=' + encodeURIComponent(pst.kw || '') +
              '&limit=' + pst.size + '&offset=' + ((pst.page - 1) * pst.size);
    fetch(url).then(function(r){
      var t = parseInt(r.headers.get('X-Total-Count') || '0', 10);
      return r.json().then(function(list){ return {list: list, total: t}; });
    }).then(function(res){
      var list = res.list;
      pst.total = res.total || 0;
      var tb = mask.querySelector('.picker-rows');
      pkSync(mask, 'picker', pst);
      if(!list.length){
        tb.innerHTML = '<tr><td colspan="7" class="muted center">' + TR('没有匹配的备件') + '</td></tr>';
        return;
      }
      var html = '';
      list.forEach(function(p){
        html += '<tr>' +
          '<td class="mono">' + esc(p.part_code) + '</td>' +
          '<td>' + esc(p.name) + '</td>' +
          '<td class="mono">' + esc(p.spec || '-') + '</td>' +
          '<td class="num">' + esc(p.stock) + ' ' + esc(p.unit) + '</td>' +
          '<td class="num">' + esc(p.unit_price) + '</td>' +
          '<td>' + (p.is_valuable ? '<span class="badge badge-val">' + TR('贵重') + '</span>' : '-') + '</td>' +
          '<td><button type="button" class="btn btn-xs btn-primary" data-id="' + p.id + '" data-code="' + esc(p.part_code) + '" data-name="' + esc(p.name) + '" data-dec="' + (p.qty_decimal||0) + '" data-unit="' + esc(p.unit||'') + '" data-stock="' + esc(p.stock||0) + '" data-val="' + (p.is_valuable||0) + '">' + TR('选择') + '</button></td>' +
          '</tr>';
      });
      tb.innerHTML = html;
      tb.querySelectorAll('button[data-id]').forEach(function(btn){
        btn.addEventListener('click', function(){
          var input = document.getElementById(curInput);
          if(input) input.value = btn.getAttribute('data-id');
          var nameEl = document.getElementById(curName);
          if(nameEl) nameEl.textContent = btn.getAttribute('data-code') + '｜' + btn.getAttribute('data-name');
          var p = {
            id: Number(btn.getAttribute('data-id')),
            part_code: btn.getAttribute('data-code'),
            name: btn.getAttribute('data-name'),
            qty_decimal: Number(btn.getAttribute('data-dec') || 0),
            unit: btn.getAttribute('data-unit') || '',
            stock: Number(btn.getAttribute('data-stock') || 0),
            is_valuable: Number(btn.getAttribute('data-val') || 0)
          };
          hide();
          if(typeof curCallback === 'function'){ var cb = curCallback; curCallback = null; cb(p); }
        });
      });
    }).catch(function(){
      mask.querySelector('.picker-rows').innerHTML = '<tr><td colspan="7" class="muted center">' + TR('搜索失败，请重试') + '</td></tr>';
    });
  }

  function show(inputId, nameId, onPick){
    curInput = inputId; curName = nameId; curCallback = onPick || null;
    ensure();
    mask.style.display = 'flex';
    var kw = mask.querySelector('.picker-kw');
    kw.value = '';
    // 每次打开从第一页开始，但沿用上次的每页条数
    pst.kw = ''; pst.page = 1; pst.total = 0;
    mask.querySelector('.picker-size').value = String(pst.size);
    kw.focus();
    search();
  }

  function hide(){ if(mask) mask.style.display = 'none'; }

  window.PartPicker = { show: show, hide: hide };

  // ============ 多选备件选择器（批量申请 / 批量出入库共用） ============
  // 用法：PartPickerMulti.show(function(parts){ ... parts 为勾选的备件数组 })
  var mmask = null, mtimer = null, mchecked = {}, mcallback = null;
  var mst = {kw: '', size: 10, page: 1, total: 0, attr: ''};

  function mensure(){
    if(mmask) return;
    mmask = document.createElement('div');
    mmask.className = 'modal-mask';
    mmask.style.display = 'none';
    mmask.addEventListener('click', function(e){ if(e.target === mmask) mhide(); });
    mmask.innerHTML =
      '<div class="modal">' +
        '<div class="modal-head"><span>' + TR('批量选择物品') + '</span>' +
          '<button type="button" class="modal-x" onclick="PartPickerMulti.hide()">✕</button></div>' +
        '<div class="modal-body">' +
          '<div class="inline-form" style="margin-bottom:10px">' +
            '<input class="f-input mpicker-kw" placeholder="' + TR('输入') + ' ' + TR('料号') + ' / ' + TR('名称') + ' / ' + TR('规格') + ' ' + TR('关键词') + '…" style="flex:1;min-width:220px">' +
            // v1.0.39：按库存所在库位属性筛选——良品/待维修/待报废一键定位，
            // 否则料号几百条翻页找「哪个在待维修区」基本找不到。
            '<select class="f-input mpicker-attr" style="max-width:120px" title="' + TR('按库存所在库位筛选') + '">' +
              '<option value="">' + TR('全部') + '</option>' +
              '<option value="good">' + TR('良品') + '</option>' +
              '<option value="pending_repair">' + TR('待维修') + '</option>' +
              '<option value="pending_scrap">' + TR('待报废') + '</option>' +
            '</select>' +
            '<button type="button" class="btn btn-primary mpicker-go">' + TR('搜索') + '</button>' +
            '<span class="muted small">' + TR('已勾选') + ' <b class="mpicker-count">0</b> ' + TR('条') + '</span>' +
          '</div>' +
          '<div class="table-wrap" style="max-height:52vh;overflow-y:auto">' +
            '<table class="table"><thead><tr><th style="width:30px"></th><th>' + TR('料号') + '</th><th>' + TR('名称') + '</th><th>' + TR('规格') + '</th><th>' + TR('库存') + '</th><th>' + TR('贵重') + '</th></tr></thead>' +
            '<tbody class="mpicker-rows"><tr><td colspan="6" class="muted center">' + TR('输入关键词搜索，或直接点击「搜索」列出全部备件') + '</td></tr></tbody>' +
          '</table></div>' +
          pkPagerBar('mpicker') +
          '<div class="modal-foot">' +
            '<button type="button" class="btn mpicker-all">' + TR('全选已列出') + '</button>' +
            '<button type="button" class="btn mpicker-clear">' + TR('清空勾选') + '</button>' +
            '<button type="button" class="btn btn-primary mpicker-ok">' + TR('确认选择') + '</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(mmask);
    var kw = mmask.querySelector('.mpicker-kw');
    // 以前 doSearch 只重置页码、没把输入框的值写回 mst.kw —— 打关键词点「搜索」
    // 拿回来的还是全部物料，等于搜索框是摆设。这里补上取值。
    function doSearch(){ mst.kw = (kw.value || '').trim(); mst.page = 1; msearch(); }
    kw.addEventListener('input', function(){ clearTimeout(mtimer); mtimer = setTimeout(doSearch, 300); });
    kw.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); doSearch(); } });
    mmask.querySelector('.mpicker-go').addEventListener('click', doSearch);
    // 属性筛选改变立刻重查（不等输入防抖）
    var mAttrSel = mmask.querySelector('.mpicker-attr');
    mAttrSel.addEventListener('change', function(){ mst.attr = this.value || ''; mst.page = 1; msearch(); });
    pkBind(mmask, 'mpicker', mst, function(){ msearch(); });
    mmask.querySelector('.mpicker-all').addEventListener('click', function(){
      mmask.querySelectorAll('input.mp-ck').forEach(function(ck){
        ck.checked = true;
        var d = JSON.parse(ck.getAttribute('data-p'));
        mchecked[d.id] = d;
      });
      mcount();
    });
    mmask.querySelector('.mpicker-clear').addEventListener('click', function(){
      mchecked = {};
      mmask.querySelectorAll('input.mp-ck').forEach(function(ck){ ck.checked = false; });
      mcount();
    });
    mmask.querySelector('.mpicker-ok').addEventListener('click', function(){
      var list = Object.keys(mchecked).map(function(k){ return mchecked[k]; });
      mhide();
      if(typeof mcallback === 'function'){ var cb = mcallback; mcallback = null; cb(list); }
    });
  }

  function mcount(){
    if(mmask) mmask.querySelector('.mpicker-count').textContent = Object.keys(mchecked).length;
  }

  function msearch(){
    var url = '/api/parts/search?kw=' + encodeURIComponent(mst.kw || '') +
              '&attr=' + encodeURIComponent(mst.attr || '') +
              '&limit=' + mst.size + '&offset=' + ((mst.page - 1) * mst.size);
    fetch(url).then(function(r){
      var t = parseInt(r.headers.get('X-Total-Count') || '0', 10);
      return r.json().then(function(list){ return {list: list, total: t}; });
    }).then(function(res){
      var list = res.list;
      mst.total = res.total || 0;
      var tb = mmask.querySelector('.mpicker-rows');
      pkSync(mmask, 'mpicker', mst);
      if(!list.length){
        tb.innerHTML = '<tr><td colspan="6" class="muted center">' + TR('没有匹配的备件') + '</td></tr>';
        return;
      }
      var html = '';
      // 待定（所有库存都没放置位置）的物料直接置灰、不给勾：
      // 以前能选上，提交时被一条红字打回来，白填一行。这里在选的时候就拦住。
      var PEND_TIP = TR('该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置');
      list.forEach(function(p){
        // v1.0.42：选「待维修/待报废」时库存列显示该类型数量，不再永远是全库位总数。
        // 服务端同时返回 stock（总量，兼容老消费方）与 attr_stock（按当前筛选口径）。
        var shown = (mst.attr && p.attr_stock !== undefined) ? Number(p.attr_stock || 0) : Number(p.stock || 0);
        var d = {id: p.id, part_code: p.part_code, name: p.name, unit: p.unit || '',
                 qty_decimal: p.qty_decimal || 0, stock: shown, total_stock: Number(p.stock || 0),
                 is_valuable: p.is_valuable || 0};
        if(p.pending){
          html += '<tr data-pid="' + p.id + '" style="opacity:.45" title="' + esc(PEND_TIP) + '">' +
            '<td><span class="badge badge-warn">' + TR('待定') + '</span></td>' +
            '<td class="mono">' + esc(p.part_code) + '</td>' +
            '<td>' + esc(p.name) + '</td>' +
            '<td class="mono">' + esc(p.spec || '-') + '</td>' +
            '<td class="num">' + esc(shown) + ' ' + esc(p.unit || '') + '</td>' +
            '<td>' + (p.is_valuable ? '<span class="badge badge-val">' + TR('贵重') + '</span>' : '-') + '</td>' +
            '</tr>';
          return;
        }
        // 勾选状态跨页保留：翻页回来仍打勾，靠 mchecked 而不是 DOM
        var ck = mchecked[d.id] ? ' checked' : '';
        html += '<tr data-pid="' + p.id + '">' +
          // v1.0.44：选框整格标记，点这一列（含复选框旁的空白）都不弹详情，避免误触
          '<td class="mp-ck-cell"><input type="checkbox" class="mp-ck"' + ck + ' data-p=\'' + JSON.stringify(d).replace(/'/g, '&#39;') + '\'></td>' +
          '<td class="mono">' + esc(p.part_code) + '</td>' +
          '<td>' + esc(p.name) + '</td>' +
          '<td class="mono">' + esc(p.spec || '-') + '</td>' +
          '<td class="num">' + esc(shown) + ' ' + esc(p.unit || '') + '</td>' +
          '<td>' + (p.is_valuable ? '<span class="badge badge-val">' + TR('贵重') + '</span>' : '-') + '</td>' +
          '</tr>';
      });
      tb.innerHTML = html;
      bindRowClick(tb);   // v1.0.40：点行看物料详情
      tb.querySelectorAll('input.mp-ck').forEach(function(ck){
        ck.addEventListener('change', function(){
          var d = JSON.parse(ck.getAttribute('data-p'));
          if(ck.checked) mchecked[d.id] = d;
          else delete mchecked[d.id];
          mcount();
        });
      });
    }).catch(function(){
      mmask.querySelector('.mpicker-rows').innerHTML = '<tr><td colspan="6" class="muted center">' + TR('搜索失败，请重试') + '</td></tr>';
    });
  }

  function mshow(onOk, opts){
    mcallback = onOk;
    mensure();
    mmask.style.display = 'flex';
    var kw = mmask.querySelector('.mpicker-kw');
    kw.value = '';
    // v1.0.46：opts.attr 预选库位属性（批量报废直接筛「待报废」）；不传则照旧全部
    var initAttr = (opts && /^(good|pending_repair|pending_scrap)$/.test(opts.attr)) ? opts.attr : '';
    mst.kw = ''; mst.page = 1; mst.total = 0; mst.attr = initAttr;
    mmask.querySelector('.mpicker-attr').value = initAttr;
    mmask.querySelector('.mpicker-size').value = String(mst.size);
    kw.focus();
    msearch();
    mcount();
  }

  function mhide(){ if(mmask) mmask.style.display = 'none'; }

  // ===== v1.0.40：批量选择弹窗里点某一行 → 物料详情 =====
  // 以前只能看「料号/名称/规格/库存/贵重」六列，同名不同规格、维修品混着的时候
  // 全靠记忆选，选错只能提交后再删。点行就能看到库存构成、库位分布、适用产线设备。
  var mdmask = null;
  function mdetailEnsure(){
    if(mdmask) return;
    mdmask = document.createElement('div');
    mdmask.className = 'modal-mask';
    mdmask.style.cssText = 'display:none;z-index:1200';
    mdmask.addEventListener('click', function(e){ if(e.target === mdmask) mdmask.style.display = 'none'; });
    mdmask.innerHTML = '<div class="modal" style="max-width:820px">' +
        '<div class="modal-head"><span>' + TR('物料详情') + '</span>' +
          '<button type="button" class="modal-x" data-close>✕</button></div>' +
        '<div class="modal-body mp-detail"></div>' +
      '</div>';
    mdmask.querySelector('[data-close]').addEventListener('click', function(){ mdmask.style.display = 'none'; });
    document.body.appendChild(mdmask);
    // v1.0.45：详情图放大预览自包含兜底——parts.html / parts_manage.html 有自己的
    // viewImg（页内 img-modal），其它页（出入库/申请/选料弹窗）没有，导致图片
    // cursor:zoom-in 却点不开（onclick 里 if(window.viewImg) 永远为假）。
    // 这里补一份全局实现；页面已有实现时不覆盖。
    if(!window.viewImg){
      var zm = document.createElement('div');
      zm.style.cssText = 'display:none;position:fixed;left:0;top:0;right:0;bottom:0;z-index:1400;'
        + 'background:rgba(15,23,42,.62);cursor:zoom-out;align-items:center;justify-content:center';
      zm.innerHTML = '<img alt="" style="max-width:92vw;max-height:92vh;display:block;'
        + 'border-radius:10px;box-shadow:0 12px 48px rgba(0,0,0,.45)">';
      zm.addEventListener('click', function(){ zm.style.display = 'none'; });
      document.body.appendChild(zm);
      window.viewImg = function(el){
        zm.querySelector('img').src = el.getAttribute('src') || '';
        zm.style.display = 'flex';
      };
    }
  }
  function mpNum(v){
    var n = Number(v || 0);
    return (Math.round(n * 1000) / 1000).toLocaleString(window.LOCALE || 'zh-CN');
  }
  function mdetail(pid){
    mdetailEnsure();
    var body = mdmask.querySelector('.mp-detail');
    body.innerHTML = '<div class="muted">' + TR('加载中…') + '</div>';
    mdmask.style.display = 'flex';
    fetch('/api/parts/' + pid).then(function(r){ return r.json(); }).then(function(d){
      if(!d || d.error){ body.innerHTML = '<div class="muted">' + esc((d && d.error) || TR('加载失败')) + '</div>'; return; }
      // 库存构成：与列表同口径（良品区良品 / 良品区维修品 / 待维修区 / 待报废区）
      var b = {good: 0, repair: 0, waitRepair: 0, waitScrap: 0};
      (d.locations || []).forEach(function(l){
        var q = Number(l.quantity) || 0;
        if(q <= 0) return;
        var a = l.attribute || 'good', c = l.condition || 'good';
        if(a === 'pending_scrap') b.waitScrap += q;
        else if(a === 'pending_repair') b.waitRepair += q;
        else if(c === 'repair') b.repair += q;
        else if(c === 'defective') b.waitRepair += q;
        else b.good += q;
      });
      var avail = b.good + b.repair, bad = b.waitRepair + b.waitScrap;
      var badge = function(txt, cls){
        return '<span class="badge badge-' + cls + '" style="margin:0 6px 6px 0;display:inline-block">' + txt + '</span>';
      };
      var mixHtml = '';
      if(b.good) mixHtml += badge(TR('良品') + ' ' + mpNum(b.good), 'ok');
      if(b.repair) mixHtml += badge(TR('维修品') + ' ' + mpNum(b.repair), 'rep');
      if(b.waitRepair) mixHtml += badge(TR('待维修品') + ' ' + mpNum(b.waitRepair), 'warn');
      if(b.waitScrap) mixHtml += badge(TR('待报废品') + ' ' + mpNum(b.waitScrap), 'danger');
      if(!mixHtml) mixHtml = '<span class="muted">' + TR('暂无库存') + '</span>';
      var seen = {}, locRows = [];
      (d.locations || []).filter(function(l){ return Number(l.quantity) > 0; }).forEach(function(l){
        var k = (l.location_id != null ? 'id:' + l.location_id : 'txt:' + (l.location || ''));
        if(!seen[k]){ seen[k] = {location: l.location || '—', attr: l.attribute || 'good', qty: 0, rep: 0}; locRows.push(k); }
        seen[k].qty += Number(l.quantity) || 0;
        seen[k].rep += Number(l.repair_qty || 0);
      });
      var locHtml = locRows.length ? locRows.map(function(k){
        var l = seen[k];
        var attr = l.attr === 'pending_repair' ? TR('待维修') : (l.attr === 'pending_scrap' ? TR('待报废') : TR('良品'));
        return '<tr><td class="mono small">' + esc(l.location) + '</td><td class="small">' + esc(attr) + '</td>'
             + '<td class="num">' + mpNum(l.qty) + (l.rep > 0 ? ' <span class="muted small">(' + TR('含维修') + ' ' + mpNum(l.rep) + ')</span>' : '') + '</td></tr>';
      }).join('') : '<tr><td colspan="3" class="muted small">' + TR('暂无库存库位') + '</td></tr>';
      var apps = (d.apps && d.apps.length) ? d.apps.map(function(a){
        return '<span class="badge" style="background:#eef4fb;color:#3b5f8a;margin:0 6px 6px 0">' + esc(a.equip_name || a.line_name || '') + '</span>';
      }).join('') : '<span class="muted">-</span>';
      var priceHtml = (d.unit_price === undefined || d.unit_price === null) ? ''
        : '<tr><th>' + TR('单价') + '</th><td class="num">' + Number(d.unit_price).toFixed(2) + ' ' + (window.CUR || '') + '</td></tr>';
      // v1.0.42：详情弹窗补物料图片（与物料信息页同一张图，走 /media/<name>）。
      // 没有图片时不留空白框，直接不渲染，避免弹窗被空图撑高。
      var imgHtml = d.image_url
        ? '<div style="float:right;margin:0 0 10px 12px;text-align:center">' +
            '<img class="detail-img" src="' + esc(d.image_url) + '" onclick="viewImg(this)" ' +
            'style="max-width:150px;max-height:150px;object-fit:contain;border:1px solid var(--line,#e2e8f0);border-radius:8px;cursor:zoom-in">' +
          '</div>'
        : '';
      body.innerHTML = imgHtml +
        '<table class="table" style="margin-bottom:10px"><tbody>' +
          '<tr><th style="width:96px">' + TR('料号') + '</th><td class="mono">' + esc(d.part_code) + '</td>' +
            '<th style="width:96px">' + TR('名称') + '</th><td>' + esc(d.name) + '</td></tr>' +
          '<tr><th>' + TR('分类') + '</th><td>' + esc(d.category || '-') + '</td>' +
            '<th>' + TR('规格') + '</th><td class="mono">' + esc(d.spec || '-') + '</td></tr>' +
          '<tr><th>' + TR('单位') + '</th><td>' + esc(d.unit || '') + '</td>' +
            '<th>' + TR('库存上下限') + '</th><td>' + mpNum(d.min_stock) + ' ~ ' + (d.max_stock ? mpNum(d.max_stock) : '∞') + '</td></tr>' +
          priceHtml +
          '<tr><th>' + TR('可用') + ' / ' + TR('不良品') + '</th><td colspan="3">' +
            '<b style="color:var(--green,#16a34a)">' + mpNum(avail) + '</b> <span class="muted small">(' + TR('良品+维修品') + ')</span>' +
            ' &nbsp;·&nbsp; <b style="color:var(--red,#dc2626)">' + mpNum(bad) + '</b> <span class="muted small">(' + TR('待维修品+待报废品') + ')</span>' +
          '</td></tr>' +
          '<tr><th>' + TR('库存构成') + '</th><td colspan="3">' + mixHtml + '</td></tr>' +
          '<tr><th>' + TR('适用产线') + '/' + TR('设备') + '</th><td colspan="3">' + apps + '</td></tr>' +
        '</tbody></table>' +
        '<div class="sub-h" style="margin:4px 0 6px">' + TR('存放库位') + '</div>' +
        '<div class="table-wrap" style="max-height:220px;overflow:auto"><table class="table"><thead><tr>' +
          '<th>' + TR('库位') + '</th><th>' + TR('库位属性') + '</th><th style="text-align:right">' + TR('库存') + '</th>' +
        '</tr></thead><tbody>' + locHtml + '</tbody></table></div>';
    }).catch(function(){
      body.innerHTML = '<div class="muted">' + TR('加载失败') + '</div>';
    });
  }
  // 点行看详情：复选框/链接等交互元素除外，避免「想勾选却弹了详情」
  function bindRowClick(tb){
    tb.querySelectorAll('tr[data-pid]').forEach(function(tr){
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', function(e){
        var t = e.target;
        if(t && (t.tagName === 'INPUT' || t.tagName === 'A' || t.tagName === 'BUTTON')) return;
        // v1.0.44：复选框那一整格（含框旁空白）都不进详情——现场反馈点选框总误弹详情
        if(t && t.closest && t.closest('.mp-ck-cell')) return;
        mdetail(tr.getAttribute('data-pid'));
      });
    });
  }

  window.PartPickerMulti = { show: mshow, hide: mhide, detail: mdetail };
})();
