/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* SPMS v1.0.31 — shared out-kind rules for 批量出入库 / 出入库调库申请.
 * Pure functions only: no Chinese UI strings (labels are translated by the page
 * with TR() and passed in), no page-specific globals. Backend mirror lives in
 * app.py::_out_cond_error + app.py::_out_cond_allow + database.py::cond_summary.
 *
 * Rules confirmed with the user (规格表 v1.0.28 + v1.0.31 修订):
 *  - 正常出库子类型只保留五类：领用 / 退货 / 内部转移 / 返还 / 报废。
 *    返修(维修)只能走「借·换·检·修」→「修(维修)」，v1.0.31 从正常出库移除。
 *  - 只有良品与维修品可以走正常出库：all_defective 时四类普通出库全部隐藏
 *  - 报废(scrap) 候选：物料在「待报废」库位有库存（has_pending_scrap）
 *  - 报废置灰与服务端 _out_cond_error 同源：只有「明确选了非待报废库位」才置灰；
 *    attr 为 null（多属性混合 / 未选库位）时不置灰，由服务端按 has_pending_scrap 判定。
 *    v1.0.30 曾把 attr!=='pending_scrap' 一律置灰，导致"良品区+待报废区都有库存"
 *    的物料无法报废——本版修复。
 *  - pending_repair 整行禁出库、pending_scrap 只剩报废可选（v1.0.24，原样保留）
 */
(function (global) {
  'use strict';

  // v1.0.31：移除 maintenance（返修/维修），正常出库只剩五类。
  // v1.0.39：新增 misc（其它），排在报废之后——领用/退货/内部转移/返还/报废都套不上的出库。
  var ORDER = ['general', 'return', 'internal', 'giveback', 'scrap', 'misc'];

  function outKindOptions(summary, attr, labels) {
    var s = summary || {};
    var allDef = !!s.all_defective;
    var hasPendingScrap = !!s.has_pending_scrap;
    var out = [];
    for (var i = 0; i < ORDER.length; i++) {
      var k = ORDER[i];
      // 规格表「只有良品与维修品可以走正常出库」：全是不良品时，
      // 领用 / 退货 / 内部转移 / 返还一律不给选，只剩报废。
      if (allDef && (k === 'general' || k === 'return' ||
                     k === 'internal' || k === 'giveback' || k === 'misc')) continue;
      // 报废：物料有待报废库位的库存才候选（v1.0.31 不再有 maintenance 候选）。
      if (k === 'scrap' && !hasPendingScrap) continue;
      var disabled = false;
      if (attr === 'pending_repair') disabled = true;                       // v1.0.24 整行禁出库
      else if (attr === 'pending_scrap' && k !== 'scrap') disabled = true;  // v1.0.24 只剩报废
      // v1.0.31：与服务端 _out_cond_error 同源——只有明确选中良品属性库位时才置灰报废；
      // attr 为 null（多属性混合/未选库位）不置灰，让服务端按 has_pending_scrap 兜底判定。
      if (k === 'scrap' && attr && attr !== 'pending_scrap') disabled = true;
      out.push({ key: k, label: (labels || {})[k] || k, disabled: disabled });
    }
    return out;
  }

  /* Keep the current selection if it is still legal; otherwise fall back to the
   * first enabled option. Used when the user changes location / condition. */
  function normalizeOutKind(summary, attr, current) {
    var opts = outKindOptions(summary, attr, {});
    var first = null, found = null;
    for (var i = 0; i < opts.length; i++) {
      if (!opts[i].disabled && first === null) first = opts[i].key;
      if (opts[i].key === current) found = opts[i];
    }
    if (found && !found.disabled) return current;
    return first || 'general';
  }

  /* Same aggregation as database.py::cond_summary. Used as a fallback for
   * drafts restored from localStorage that predate v1.0.27. */
  function fromLocations(locs) {
    var rows = [], i, l;
    for (i = 0; i < (locs || []).length; i++) {
      l = locs[i];
      if (Number(l.quantity) > 0) rows.push(l);
    }
    var conds = rows.map(function (x) { return x.condition || 'good'; });
    var attrs = rows.map(function (x) { return x.attribute || 'good'; });
    return {
      rows: rows.length,
      has_defective: conds.some(function (c) { return c === 'defective'; }),
      all_defective: rows.length > 0 && conds.every(function (c) { return c === 'defective'; }),
      has_pending_scrap: attrs.some(function (a) { return a === 'pending_scrap'; }),
      has_pending_repair: attrs.some(function (a) { return a === 'pending_repair'; })
    };
  }

  function summary(row) {
    if (row && row.cond_summary && typeof row.cond_summary === 'object') return row.cond_summary;
    return fromLocations((row && row.locs) || []);
  }

  /* Deduction location attribute. Prefer the explicitly selected location;
   * otherwise, if every in-stock row shares one attribute, use it. */
  function inferAttr(locs, selectedId) {
    var i, l, sid = (selectedId === null || selectedId === undefined) ? '' : String(selectedId);
    if (sid && sid !== '__NEW__' && sid !== 'PENDING') {
      for (i = 0; i < (locs || []).length; i++) {
        l = locs[i];
        if (String(l.location_id) === sid) return l.attribute || 'good';
      }
    }
    var attrs = {}, count = 0, only = null;
    for (i = 0; i < (locs || []).length; i++) {
      l = locs[i];
      if (Number(l.quantity) > 0 && Number(l.location_id)) {
        var a = l.attribute || 'good';
        if (!attrs[a]) { attrs[a] = 1; count++; only = a; }
      }
    }
    return count === 1 ? only : null;
  }

  /* v1.0.42：按所选类型返回该类型的可用数量（四分类口径，与后端 _query_parts 同源）。
   * kind: 'good'（良品+维修品，默认）/ 'repair'（良品区维修品）/ 'maintenance'（待维修品）/
   *       'scrap'（待报废品）/ 'all'（全库位总数，仅兼容老调用）。
   * 待维修品 = 待维修区 + 良品区 condition=defective；待报废品 = 待报废区。
   * 维修品（repair）是良品区第三态，属于「可用」的一部分。 */
  function stockOf(locs, kind) {
    var b = {good: 0, repair: 0, waitRepair: 0, waitScrap: 0};
    for (var i = 0; i < (locs || []).length; i++) {
      var l = locs[i], q = Number(l.quantity) || 0;
      if (q <= 0) continue;
      var a = l.attribute || 'good', c = l.condition || 'good';
      if (a === 'pending_scrap') b.waitScrap += q;
      else if (a === 'pending_repair') b.waitRepair += q;
      else if (c === 'repair') b.repair += q;
      else if (c === 'defective') b.waitRepair += q;
      else b.good += q;
    }
    // v1.0.45：维修（借·换·检·修 → 修）可从「待维修 + 待报废」库位取件（维修抢救，
    // 用户拍板开放；库位规则层 _loc_out_allowed 本就允许 pending_scrap+维修）。
    // 'waitRepair' 仍=纯待维修口径（库存构成展示用），'maintenance' 是维修出库取数口径。
    if (kind === 'maintenance') return b.waitRepair + b.waitScrap;
    if (kind === 'waitRepair') return b.waitRepair;
    if (kind === 'scrap' || kind === 'waitScrap') return b.waitScrap;
    if (kind === 'repair') return b.repair;
    if (kind === 'all') return b.good + b.repair + b.waitRepair + b.waitScrap;
    return b.good + b.repair; // good / 默认：良品+维修品
  }

  global.CondRules = {
    ORDER: ORDER,
    outKindOptions: outKindOptions,
    normalizeOutKind: normalizeOutKind,
    fromLocations: fromLocations,
    summary: summary,
    inferAttr: inferAttr,
    stockOf: stockOf
  };
})(window);
