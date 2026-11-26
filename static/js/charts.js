/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* 轻量 Canvas 图表（无外部依赖，内网离线可用）
 * drawBars / drawLines / drawPie 三个函数。
 * 可选 opts（大屏深色时传入）：
 *   { font, text, grid, line, sub, value, bg }
 */
(function(){
  const tip = document.createElement('div');
  tip.style.cssText = 'position:fixed;background:rgba(17,24,39,.92);color:#fff;padding:6px 10px;'+
    'border-radius:6px;font-size:12px;pointer-events:none;display:none;z-index:9999;white-space:nowrap;';
  document.body.appendChild(tip);

  function showTip(x, y, html){
    tip.innerHTML = html;
    tip.style.display = 'block';
    let tx = x + 14, ty = y - 8;
    if(tx + tip.offsetWidth > window.innerWidth) tx = x - tip.offsetWidth - 10;
    if(ty < 8) ty = 8;
    tip.style.left = tx + 'px'; tip.style.top = ty + 'px';
  }
  function hideTip(){ tip.style.display = 'none'; }

  function setup(canvas){
    const dpr = window.devicePixelRatio || 1;
    let w = canvas.clientWidth || canvas.parentElement.clientWidth;
    let h = canvas.clientHeight || 260;
    // canvas 没有 CSS 高度时，显示高度就跟着 height 属性走；下面把属性设成 h*dpr
    // （高分屏是 2 倍），显示高度也跟着变 2 倍，下次再量到的 clientHeight 又是新的
    // 2 倍 —— 画一次涨一次，饼图能长到设计要求的两倍高。宽度同理。
    // 所以先把「这一次量到的 CSS 尺寸」钉进内联样式，之后改属性只影响绘制精度，
    // 不再反过来撑大版面。模板里已经写了 style 的（趋势图）保持不动。
    if(!canvas.style.height) canvas.style.height = h + 'px';
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    // 二次校准：改完属性后 CSS 宽度可能已经不是刚才量的那个数（图表所在卡片刚
    // 由半宽切成整行、或从隐藏变为显示）。位图和显示尺寸不等比，圆圈就会被压成
    // 椭圆 —— 分析页那张饼图「扁了」就是这么来的。这里再量一次，不一致就按新尺寸重设。
    const w2 = canvas.clientWidth, h2 = canvas.clientHeight;
    if(w2 > 0 && h2 > 0 && (Math.abs(w2 - w) > 1 || Math.abs(h2 - h) > 1)){
      w = w2; h = h2;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return {ctx, w, h};
  }

  // 空数据占位：必须清空画布，否则切换筛选条件后会把上一次的图留在原地，
  // 造成"数量图有旧数据、金额图空白"这种自相矛盾的画面（见 v2.9.17 修复）
  function noData(canvas){
    if(!canvas) return;
    const {ctx, w, h} = setup(canvas);
    const msg = (typeof window.TR === 'function') ? window.TR('暂无数据') : '暂无数据';
    const f = Math.max(11, Math.min(14, Math.round(w / 40)));
    ctx.fillStyle = D.sub; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = f + 'px "Microsoft YaHei",sans-serif';
    ctx.fillText(msg, w / 2, h / 2);
    ctx.textBaseline = 'alphabetic';
  }

  // 根据 canvas 高度自动选字号：卡片小→字小，避免标签重叠
  function autoFont(o, h){
    const f = opt(o,'font');
    if(h < 140) return Math.min(f, 10);
    if(h < 200) return Math.min(f, 11);
    if(h < 280) return Math.min(f, 12);
    return f;
  }
  // 根据最大刻度文字宽度动态算左边距
  function autoLeftM(ctx, maxVal, font){
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';
    const s = fmtNum(maxVal);
    return Math.max(36, ctx.measureText(s).width + 14);
  }

  const D = {
    font: 12, text: '#475569', grid: '#eef1f5', line: '#dde3ea',
    bottomVal: false,
    sub: '#6b7280', value: '#6b7280', bg: '#ffffff'
  };
  // 默认彩色色板（避免数据未带颜色时画出白/灰柱子）
  // 默认色板（数据没带颜色时用）：冷色阶梯 + 一个暖色，同一套配色语言，
  // 不再用「蓝绿紫青橙粉」这种彩虹轮转 —— 那张图一看就是默认样式。
  const PALETTE = ['#3b82f6','#22c55e','#8b5cf6','#06b6d4','#f59e0b','#ec4899','#84cc16','#14b8a6','#6366f1','#0ea5e9'];
  function opt(o, k){ return o && o[k] !== undefined ? o[k] : D[k]; }

  function niceMax(v){
    if(v <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / exp;
    const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nf * exp;
  }

  /* 轴刻度步长：从阶梯里挑一个能把「区间分成 3 段」的整数步长。
     组合图左右轴都画 4 个刻度（0 25% 50% 75% 100%），若先定上限再除 3，
     就会出现 17/33、1,667/3,333 这种刻度 —— 每读一格都得心算。
     fine=true 时用更密的一档阶梯（金额轴用）。 */
  function niceStep(v, fine){
    if(!(v > 0)) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / exp;
    const steps = fine
      ? [1, 1.2, 1.5, 1.8, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8, 9, 10]
      : [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for(let i = 0; i < steps.length; i++){
      if(f <= steps[i] + 1e-9) return steps[i] * exp;
    }
    return 10 * exp;
  }

  /* 纵轴上限：先 niceMax 再 /3 会得到 1,667 / 3,333 这种刻度，读图还得心算。
     这里把「步长」先取整，上限 = 步长 x 段数，刻度就永远是 0/2,000/4,000/6,000 这种。 */
  function axisMax(v, ticks){
    let step = niceStep(niceMax(Math.max(1, v)) / ticks, false);
    if(step < 1) step = 1;
    return step * ticks;
  }

  window.drawBars = function(id, labels, series, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    if(!labels || !labels.length || !series || !series.length){ noData(canvas); return; }
    const {ctx, w, h} = setup(canvas);
    const font = autoFont(o, h), sub = opt(o,'sub'), text = opt(o,'text'),
          grid = opt(o,'grid'), line = opt(o,'line');
    const n = labels.length;
    // 横轴标签：表格样式——每个柱位下方水平换行完整显示标签；可选底部数值行（bottomVal，默认开）
    const lf = font, lineH = lf + 6, bottomVal = opt(o,'bottomVal') !== false;
    const maxLen = Math.max.apply(null, labels.map(function(l){ return Math.max.apply(null, String(l).split('\n').map(function(s){ return s.length; })); }));
    const maxVal = axisMax(Math.max(1, ...series.flatMap(s=>s.data)), 3);
    const cpl = Math.max(2, Math.floor(((w - 68) / n - 10) / (lf * 0.62)));   // 每行最多字符数
    const maxLines = Math.max(1, Math.ceil(maxLen / cpl));
    let bRot = 10 + maxLines * lineH + lf + 12;
    bRot = Math.max(30, Math.min(bRot, Math.floor(h * 0.45)));
    const M = {l: 48, r: 12, t: 16, b: bRot};
    const pw = w - M.l - M.r, ph = h - M.t - M.b;
    const yFont = Math.max(9, font - 1);
    ctx.font = yFont + 'px "Microsoft YaHei",sans-serif';
    const tickCount = 3;
    for(let i=0;i<=tickCount;i++){
      const v = maxVal*i/tickCount, y = M.t + ph - ph*i/tickCount;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(w-M.r, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = 'right';
      ctx.fillText(fmtNum(v), M.l-6, y+yFont*0.35);
    }
    ctx.strokeStyle = line;
    ctx.beginPath(); ctx.moveTo(M.l, M.t+ph); ctx.lineTo(w-M.r, M.t+ph); ctx.stroke();
    const groupW = pw/n, slotW = groupW / series.length;
    // 标签表格：每柱位下方水平居中、自动换行完整显示；底部一行数值（悬停 tooltip 显示全名）
    ctx.fillStyle = text; ctx.textAlign = 'center';
    ctx.font = lf + 'px "Microsoft YaHei",sans-serif';
    // 按空格断词换行，优先保持"名称 规格"完整成词；超长词再按字符硬切
    const wrapLines = function(txt){
      const lines = [];
      String(txt).split('\n').forEach(function(seg){
        if(!seg){ lines.push(''); return; }
        const words = String(seg).split(/(\s+)/);
        let cur = '';
        words.forEach(function(w){
          if(!w) return;
          if(/^\s+$/.test(w)){ cur += w; return; }          // 保留词间空格
          const test = cur + w;
          if(cur && ctx.measureText(test).width > groupW - 10){ lines.push(cur.trim()); cur = w; }
          else cur = test;
        });
        if(cur.trim()) lines.push(cur.trim());
        // 超长词按字符硬切，保证不溢出列宽
        for(let li=0; li<lines.length; li++){
          let l = lines[li];
          while(ctx.measureText(l).width > groupW - 10){
            let cut = l.length - 1;
            while(cut > 0 && ctx.measureText(l.slice(0, cut)).width > groupW - 10) cut--;
            if(cut <= 0) break;
            lines.splice(li + 1, 0, l.slice(cut));
            l = l.slice(0, cut); lines[li] = l;
          }
        }
      });
      return lines;
    };
    labels.forEach((lb,i)=>{
      const cx = M.l + groupW*i + groupW/2;
      const lines = wrapLines(lb);
      const startY = h - bRot + 8;
      lines.forEach(function(ln, li){
        ctx.fillText(ln, cx, startY + li*lineH + lf);
      });
    });
    // 底部数值行（各柱位合计，可关闭避免遮挡标签）
    if(bottomVal){
      ctx.fillStyle = sub;
      labels.forEach(function(lb,i){
        const cx = M.l + groupW*i + groupW/2;
        const v = series.reduce(function(s,se){ return s + (se.data[i]||0); }, 0);
        ctx.fillText(fmtNum(v), cx, h - 4);
      });
    }
    ctx.fillStyle = text; ctx.textAlign = 'right';
    // 数值标签防重叠：卡片矮/日期多时，14 组柱子的「3,415」会比柱位还宽，
    // 全画出来就是一片糊。这里按已画标签的右边界让位，挤不下的直接不画
    // （精确值悬停有 tooltip，纵轴有刻度）。
    let valRight = -1e9;
    series.forEach((s, si)=>{
      const sColor = s.color || PALETTE[si % PALETTE.length];
      s.data.forEach((v,i)=>{
        const x = M.l + groupW*i + slotW*si + slotW*0.15;
        const bh = (v/maxVal)*ph;
        const y = M.t + ph - bh;
        ctx.fillStyle = sColor;
        ctx.fillRect(x, y, slotW*0.7, bh);
        if(v>0){
          ctx.fillStyle = sub; ctx.textAlign='center'; ctx.font=Math.max(9,font-1)+'px sans-serif';
          const vt = fmtNum(v), vw = ctx.measureText(vt).width;
          const vcx = x + slotW*0.35;
          if(vcx - vw/2 >= valRight + 5){
            ctx.fillText(vt, vcx, y - 3);   // 标签永远在柱子正上方 3px
            valRight = vcx + vw/2;
          }
        }
      });
    });
    // 单系列不画图例：名字和卡片标题重复，挤在右上角只会显得乱
    if(series.length > 1) legend(ctx, w, series.map(s=>({name:s.name, color:s.color})), font, sub, true);
    canvas.onmousemove = e=>{
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      let hit = null;
      series.forEach((s, si)=>{
        s.data.forEach((v,i)=>{
          const bx = M.l + groupW*i + slotW*si + slotW*0.15;
          if(x>=bx && x<=bx+slotW*0.7 && y<=M.t+ph && y>=M.t){
            hit = {i, si, v, lb: labels[i], name: s.name};
          }
        });
      });
      if(hit){
        canvas.style.cursor='pointer';
        showTip(e.clientX, e.clientY, '<b>'+esc(hit.lb)+'</b> '+esc(hit.name)+(window.COLON||'：')+fmtNum(hit.v));
      } else { canvas.style.cursor='default'; hideTip(); }
    };
    canvas.onmouseleave = hideTip;
  };

  /* v1.0.41 盘点计划与实盘看板图（横轴改为自然时间比例）。
   * nodes: [{label(月份), ok(数量无误), bad(数量有误), from, to, plan_base, state,
   *          done_date, cause, action, diff_rows, amount, current}]
   * 视觉口径（v1.0.66 简洁平面改版）：
   *   · x 坐标按真实日期线性映射：周期天数/容差一改，柱间距和窗口宽度立刻跟着变
   *   · 淡蓝 = 盘点期间（from ~ to）；仅留边界虚线与两端日期，不叠玻璃面板/立体台面
   *   · 每期两根平面渐变柱：蓝=数量无误，红=数量有误；柱顶直接写数值，不加圈/胶囊
   *   · 时间推进基线、「今天」字样与红色竖虚线均已按现场反馈移除
   */
  window.drawStkBoard = function(id, nodes, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    if(!nodes || !nodes.length){ noData(canvas); return; }
    const {ctx, w, h} = setup(canvas);
    o = o || {};
    const font = Math.max(11, Math.min(14, Math.round(w / 58)));
    const cBlue = '#3b82f6', cRed = '#ef4444', cGreen = '#22c55e';
    const cText = opt(o,'text'), cGrid = opt(o,'grid'), cSub = opt(o,'sub'), cLine = opt(o,'line');
    // v1.0.66：回到简洁平面风格，顶部仍留一行区间标签，边距收紧
    const M = {l: 46, r: 20, t: 56, b: 46};
    const roundRect = function(c, x, y, ww, hh, r){
      r = Math.max(0, Math.min(r, ww/2, hh/2));
      c.beginPath(); c.moveTo(x+r, y);
      c.arcTo(x+ww, y, x+ww, y+hh, r);
      c.arcTo(x+ww, y+hh, x, y+hh, r);
      c.arcTo(x, y+hh, x, y, r);
      c.arcTo(x, y, x+ww, y, r);
      c.closePath();
    };
    // 竖向渐变只用于柱身（参考图的平面渐变柱），不再做立体顶盖/侧面
    const vgrad = function(c, y0, y1, stops){
      const gr = c.createLinearGradient(0, y0, 0, y1);
      stops.forEach(function(s){ gr.addColorStop(s[0], s[1]); });
      return gr;
    };
    const pw = w - M.l - M.r, ph = h - M.t - M.b;
    const n = nodes.length;
    const maxV = axisMax(Math.max(1, ...nodes.flatMap(d => [d.ok||0, d.bad||0])), 4);
    const yOf = v => M.t + ph - (v / maxV) * ph;
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';

    // 日期 → 天序号。横轴宽度与自然时间一致，不再按节点数均分。
    const dayOf = function(s){
      const p = String(s || '').slice(0, 10).split('-');
      if(p.length < 3 || !(+p[0]) || !(+p[1]) || !(+p[2])) return NaN;
      return Date.UTC(+p[0], +p[1]-1, +p[2]) / 86400000;
    };
    const stepDays = Math.max(1, parseInt(o.cycleDays, 10) || 30);
    const g = nodes.map(function(d, i){
      const f = dayOf(d.from), t = dayOf(d.to), pb = dayOf(d.plan_base || d.label);
      // v1.0.55：柱子落在「实际盘点日」bar_date（未盘期仍是计划日），
      // 区间标签仍用 from~to，两者分开，柱子不会再钉在区间中心。
      const bd = dayOf(d.bar_date || d.done_date);
      const fallback = isFinite(pb) ? pb : (isFinite(f) ? f + stepDays/2 : i * stepDays);
      const base = isFinite(bd) ? bd : fallback;
      return {d: d, f: isFinite(f) ? f : base - stepDays/5,
              t: isFinite(t) ? t : base + stepDays/5, b: base};
    });
    let dMin = Math.min.apply(null, g.map(x => Math.min(x.f, x.b)));
    let dMax = Math.max.apply(null, g.map(x => Math.max(x.t, x.b)));
    if(!isFinite(dMin) || !isFinite(dMax) || dMax <= dMin){ dMin = 0; dMax = stepDays * n; }
    dMin -= 0.5; dMax += 0.5;                 // 两侧留半格，虚线不贴边
    const xOfDay = day => M.l + (day - dMin) / (dMax - dMin) * pw;
    const cxOf = i => xOfDay(g[i].b);

    // 底纹：只保留极淡的盘点窗口色块，参考图那种干净白底 + 细横网格
    g.forEach(function(x){
      const xl = xOfDay(x.f), xr = xOfDay(x.t);
      ctx.fillStyle = o.dark === true ? 'rgba(96,165,250,.10)' : 'rgba(219,234,254,.42)';
      ctx.fillRect(xl, M.t, Math.max(3, xr - xl), ph);
    });

    // 纵轴刻度：浅灰实线，不画阴影、不画台面
    for(let i=0;i<=4;i++){
      const v = maxV*i/4, y = yOf(v);
      ctx.strokeStyle = o.dark === true ? 'rgba(148,163,184,.20)' : 'rgba(148,163,184,.28)';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(w-M.r, y); ctx.stroke();
      ctx.fillStyle = cSub; ctx.textAlign = 'right';
      ctx.fillText(fmtNum(v), M.l-8, y + font*0.35);
    }
    ctx.strokeStyle = cLine;
    ctx.beginPath(); ctx.moveTo(M.l, M.t+ph); ctx.lineTo(w-M.r, M.t+ph); ctx.stroke();

    // v1.0.45：删除原「时间推进基线」（贯穿横线+每期绿点+末端箭头）——
    // 现场反馈图中的直线不要了；本期位置改由后端窗口右移保证（倒数第二列），
    // 各期区间与当前期标记已足够传达时间推进。cxOf 仍被横轴日期使用，保留。

    // v1.0.43：每期只占一行——左端下限日期、右端上限日期，同高对齐；
    // 中间用一条带箭头的区间线连起来，取代上下错开的两行标签。
    const rangeName = o.rangeLabelName || TR('盘点区间');
    const labFont = Math.max(10, Math.min(12, font - 1));
    const mdOf = function(s){
      const p = String(s || '').slice(0, 10).split('-');
      return p.length >= 3 ? (parseInt(p[1], 10) + '/' + parseInt(p[2], 10)) : '';
    };
    ctx.font = labFont + 'px "Microsoft YaHei",sans-serif';
    const labelY = M.t - 14;
    let lastLabelRight = M.l - 99;
    g.forEach(function(x){
      const xl = xOfDay(x.f), xr = xOfDay(x.t);
      // 窗口边界：一条淡虚线，不再叠色块/高光
      ctx.save();
      ctx.setLineDash([3,4]);
      ctx.strokeStyle = o.dark === true ? 'rgba(147,197,253,.45)' : 'rgba(148,163,184,.45)';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xl, M.t); ctx.lineTo(xl, M.t+ph); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(xr, M.t); ctx.lineTo(xr, M.t+ph); ctx.stroke();
      ctx.restore();
      const lt = mdOf(x.d.from), rt = mdOf(x.d.to);
      const lw = ctx.measureText(lt).width, rw = ctx.measureText(rt).width;
      if(xl - lw/2 < lastLabelRight + 4) return;  // 窗太密就不画，避免糊成一片
      lastLabelRight = xr + rw/2 + 4;
      const a1 = xl + lw/2 + 3, a2 = xr - rw/2 - 3;
      const mid = (xl + xr) / 2;
      const mw = ctx.measureText(rangeName).width;
      // 区间箭头 + 平铺文字：无胶囊、无阴影，贴近参考图的简洁度
      if(a2 - a1 > 12){
        ctx.save();
        ctx.strokeStyle = o.dark === true ? '#60a5fa' : '#93c5fd';
        ctx.fillStyle = ctx.strokeStyle;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(a1, labelY); ctx.lineTo(a2-5, labelY); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(a2, labelY);
        ctx.lineTo(a2-5, labelY-3); ctx.lineTo(a2-5, labelY+3); ctx.closePath(); ctx.fill();
        ctx.restore();
      }
      if(xr - xl > mw + (lw + rw) / 2 + 10){
        ctx.fillStyle = o.dark === true ? '#0f172a' : '#ffffff';
        ctx.fillRect(mid - mw/2 - 3, labelY - labFont, mw + 6, labFont + 3);
        ctx.fillStyle = o.dark === true ? '#93c5fd' : '#64748b';
        ctx.textAlign = 'center';
        ctx.fillText(rangeName, mid, labelY);
      }
      ctx.fillStyle = o.dark === true ? '#bfdbfe' : '#1e3a8a';
      ctx.textAlign = 'center';
      ctx.fillText(lt, xl, labelY);
      ctx.fillText(rt, xr, labelY);
    });
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';

    // 平面渐变双柱（参考图风格）：柱心落在 bar_date（未盘期为计划日）的自然时间坐标上
    // v1.0.73：按现场反馈柱宽上限 24→34、占比 0.24→0.30，并给柱身加柔和落影。
    const barW = Math.max(12, Math.min(34, pw / Math.max(n, 1) * 0.30));
    const pal = [
      {top:'#60a5fa', bot:'#2563eb', txt:'#1d4ed8'},
      {top:'#fb7185', bot:'#e11d48', txt:'#be123c'}
    ];
    g.forEach(function(x){
      const cx = xOfDay(x.b);
      const ok = Math.max(0, x.d.ok||0), bad = Math.max(0, x.d.bad||0);
      [[cx-barW-2, ok, 0],[cx+2, bad, 1]].forEach(function(bar){
        const bx = bar[0], bv = bar[1], p = pal[bar[2]];
        if(bv <= 0) return;
        const baseY = M.t + ph;
        const by = Math.max(M.t + 18, yOf(bv));
        // 一根柱：竖向渐变矩形 + 顶部小圆角 + 柔和落影（v1.0.73）
        ctx.save();
        ctx.shadowColor = o.dark === true ? 'rgba(0,0,0,0.55)' : 'rgba(30,64,175,0.28)';
        ctx.shadowBlur = 12;
        ctx.shadowOffsetY = 5;
        ctx.fillStyle = vgrad(ctx, by, baseY, [[0,p.top],[1,p.bot]]);
        roundRect(ctx, bx, by, barW, Math.max(3, baseY - by), 3); ctx.fill();
        ctx.restore();   // 落影不能糊到数值/坐标轴文字上
        const vt = fmtNum(bv);
        ctx.font = '700 ' + (labFont+1) + 'px "Microsoft YaHei",sans-serif';
        ctx.textAlign = 'center';
        ctx.fillStyle = o.dark === true ? (bar[2] === 0 ? '#bfdbfe' : '#fda4af') : p.txt;
        ctx.fillText(vt, bx + barW/2, Math.max(M.t + labFont, by - 6));
      });
    });
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';

    // 横轴日期：按自然时间排布，密集时隔一个显示，但当前期一定显示
    const minGap = 52;
    let lastLabelX = -1e9;
    g.forEach(function(x, i){
      const cx = xOfDay(x.b);
      if(cx - lastLabelX < minGap && !x.d.current) return;
      lastLabelX = cx;
      ctx.font = (x.d.current ? '700 ' : '') + labFont + 'px "Microsoft YaHei",sans-serif';
      // 横轴日期跟柱子走：已盘显示实际盘点日，未盘才显示计划日。v1.0.66 去掉胶囊与阴影。
      const ml = x.d.done_date ? String(x.d.done_date).slice(5) : String(x.d.label||'');
      ctx.fillStyle = x.d.current ? (o.dark === true ? '#bfdbfe' : '#2563eb') : cText;
      ctx.textAlign = 'center';
      ctx.fillText(ml, cx, h - 16);
      ctx.font = font + 'px "Microsoft YaHei",sans-serif';
    });

    // v1.0.65：按现场反馈移除底部独立日期标记与红色竖虚线；
    // 横轴只保留各期完成/计划日胶囊，本期用主色描边区分。

    // hover：按柱心的自然时间距离命中（比旧的等宽分格更贴合视觉）
    canvas.onmousemove = function(e){
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      let hit = null;
      if(my >= M.t && my <= M.t+ph){
        let best = 1e9;
        g.forEach(function(x, i){
          const dist = Math.abs(mx - xOfDay(x.b));
          if(dist < best && dist <= 46){ best = dist; hit = nodes[i]; }
        });
      }
      if(hit){
        // v1.0.56：未来期还没开始盘点，不提供点击进入报表的入口
        canvas.style.cursor = hit.state === 'not_due' ? 'default' : 'pointer';
        const stName = (o.stateNames && o.stateNames[hit.state]) || hit.state || '';
        let html = '<b>'+esc(hit.plan_base||hit.label||'')+'</b> '+esc(stName)
          + '<br>'+esc(o.okName||TR('数量无误'))+(window.COLON||'：')+fmtNum(hit.ok||0)
          + '　'+esc(o.badName||TR('数量有误'))+(window.COLON||'：')+fmtNum(hit.bad||0)
          + '<br>'+esc(o.rangeName||TR('盘点时间范围'))+(window.COLON||'：')+esc(hit.from||'')+' ~ '+esc(hit.to||'');
        if(hit.done_date) html += '<br>'+esc(o.doneName||TR('完成日'))+(window.COLON||'：')+esc(hit.done_date);
        // v1.0.55：未来期还没到，只显示「未到盘点期」，不提前记未盘
        if(hit.state !== 'not_due' && hit.uncovered != null && hit.uncovered > 0)
          html += '<br>'+esc(o.uncoveredName||TR('未盘'))+(window.COLON||'：')+fmtNum(hit.uncovered);
        if(hit.report_url && hit.state !== 'not_due') html += '<br><span style="opacity:.75">'+esc(o.clickHint||TR('点击查看该期报表'))+'</span>';
        showTip(e.clientX, e.clientY, html);
      } else { canvas.style.cursor='default'; hideTip(); }
    };
    canvas.onmouseleave = hideTip;
    // v1.0.51：点柱子进入对应盘点报表（未盘/未来期进该期记录页，能看到未盘数）
    canvas.onclick = function(e){
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      if(my < M.t || my > M.t+ph) return;
      let hit = null, best = 1e9;
      g.forEach(function(x, i){
        const dist = Math.abs(mx - xOfDay(x.b));
        if(dist < best && dist <= 46){ best = dist; hit = nodes[i]; }
      });
      if(hit && hit.state !== 'not_due' && hit.report_url) window.location.href = hit.report_url;
    };
  };

  window.drawLines = function(id, labels, series, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    if(!labels || !labels.length || !series || !series.length){ noData(canvas); return; }
    const {ctx, w, h} = setup(canvas);
    const font = autoFont(o, h), text = opt(o,'text'), grid = opt(o,'grid'),
          line = opt(o,'line'), sub = opt(o,'sub'), value = opt(o,'value');
    const n = labels.length;
    const lf = font, lineH = lf + 6, bottomVal = opt(o,'bottomVal') !== false;
    const maxLen = Math.max.apply(null, labels.map(function(l){ return Math.max.apply(null, String(l).split('\n').map(function(s){ return s.length; })); }));
    const maxVal = axisMax(Math.max(1, ...series.flatMap(s=>s.data)), 3);
    const cpl = Math.max(2, Math.floor(((w - 68) / n - 10) / (lf * 0.62)));
    const maxLines = Math.max(1, Math.ceil(maxLen / cpl));
    let bRot = 10 + maxLines * lineH + lf + 12;
    bRot = Math.max(30, Math.min(bRot, Math.floor(h * 0.45)));
    const M = {l: 48, r: 12, t: 16, b: bRot};
    const pw = w - M.l - M.r, ph = h - M.t - M.b;
    const yFont = Math.max(9, font - 1);
    ctx.font = yFont + 'px "Microsoft YaHei",sans-serif';
    const tickCount = 3;
    for(let i=0;i<=tickCount;i++){
      const v = maxVal*i/tickCount, y = M.t + ph - ph*i/tickCount;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(w-M.r, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = 'right';
      ctx.fillText(fmtNum(v), M.l-6, y+yFont*0.35);
    }
    ctx.strokeStyle = line;
    ctx.beginPath(); ctx.moveTo(M.l, M.t+ph); ctx.lineTo(w-M.r, M.t+ph); ctx.stroke();
    const step = pw/(n-1||1);
    // 标签表格：每点下方水平居中、自动换行完整显示；底部一行数值
    ctx.fillStyle = text; ctx.textAlign = 'center';
    ctx.font = lf + 'px "Microsoft YaHei",sans-serif';
    // 标签放不下就抽稀：日期这种定长串被硬切成「10-2 / 0」比少画几个还难读。
    // 只有真挤（相邻点距 < 标签宽）才抽，宽屏还是每个点都标。
    let maxLblW = 0;
    labels.forEach(function(lb){ maxLblW = Math.max(maxLblW, ctx.measureText(String(lb)).width); });
    const lblStep = Math.max(1, Math.ceil((maxLblW + 8) / Math.max(1, step)));
    const labelW = Math.max(step - 8, step * lblStep - 8);   // 抽稀后每个标签真正可用的宽度
    const shownIdx = [];
    for(let i = 0; i < n; i += lblStep) shownIdx.push(i);
    if(n > 1 && shownIdx[shownIdx.length - 1] !== n - 1){
      if((n - 1 - shownIdx[shownIdx.length - 1]) * step >= maxLblW + 6) shownIdx.push(n - 1);
      else shownIdx[shownIdx.length - 1] = n - 1;
    }
    // 按空格断词换行，优先保持"名称 规格"完整成词；超长词再按字符硬切
    const wrapLines = function(txt){
      const lines = [];
      String(txt).split('\n').forEach(function(seg){
        if(!seg){ lines.push(''); return; }
        const words = String(seg).split(/(\s+)/);
        let cur = '';
        words.forEach(function(w){
          if(!w) return;
          if(/^\s+$/.test(w)){ cur += w; return; }          // 保留词间空格
          const test = cur + w;
          if(cur && ctx.measureText(test).width > labelW){ lines.push(cur.trim()); cur = w; }
          else cur = test;
        });
        if(cur.trim()) lines.push(cur.trim());
        for(let li=0; li<lines.length; li++){
          let l = lines[li];
          while(ctx.measureText(l).width > labelW){
            let cut = l.length - 1;
            while(cut > 0 && ctx.measureText(l.slice(0, cut)).width > labelW) cut--;
            if(cut <= 0) break;
            lines.splice(li + 1, 0, l.slice(cut));
            l = l.slice(0, cut); lines[li] = l;
          }
        }
      });
      return lines;
    };
    labels.forEach((lb,i)=>{
      if(shownIdx.indexOf(i) < 0) return;
      // 末端标签居中在绘图区右边界上，会有一半跑到卡片外边被裁掉，这里夹回画布内
      const lines = wrapLines(lb);
      const widest = Math.max.apply(null, lines.map(function(l){ return ctx.measureText(l).width; }));
      const cx = Math.max(M.l, Math.min(M.l + step*i, w - 4 - widest/2));
      const startY = h - bRot + 8;
      lines.forEach(function(ln, li){
        ctx.fillText(ln, cx, startY + li*lineH + lf);
      });
    });
    if(bottomVal){
      ctx.fillStyle = sub;
      shownIdx.forEach(function(i){
        const cx = M.l + step*i;
        const v = series.reduce(function(s,se){ return s + (se.data[i]||0); }, 0);
        ctx.fillText(fmtNum(v), cx, h - 4);
      });
    }
    ctx.fillStyle = text; ctx.textAlign = 'right';
    const pts = [];
    const valLabels = [];
    series.forEach((s, si)=>{
      const sColor = s.color || PALETTE[si % PALETTE.length];
      ctx.strokeStyle = sColor; ctx.lineWidth = 2; ctx.beginPath();
      s.data.forEach((v,i)=>{
        const x = M.l+step*i, y = M.t+ph-(v/maxVal)*ph;
        i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
        pts.push({x, y, v, i, name:s.name, lb:labels[i]});
      });
      ctx.stroke();
      ctx.fillStyle = sColor;
      s.data.forEach((v,i)=>{
        ctx.beginPath(); ctx.arc(M.l+step*i, M.t+ph-(v/maxVal)*ph, 3, 0, Math.PI*2); ctx.fill();
      });
      // 数值标注先收集、后统一绘制：两条线各自的标签如果各画各的，
      // 一条全 0（入库没量）另一条有数，就会在同一个 x 上叠成「870/318」这种鬼影。
      ctx.font = Math.max(9,font-1) + 'px "Microsoft YaHei",sans-serif';
      s.data.forEach((v,i)=>{
        if(shownIdx.indexOf(i) < 0) return;
        if(!(v > 0)) return;                       // 0 不标：金额图里 0 一多就是一排「0」
        const x = M.l+step*i, y = M.t+ph-(v/maxVal)*ph;
        let vy = Math.max(M.t+font+6, Math.min(y-4, h - bRot + 6));
        const tw = ctx.measureText(fmtNum(v)).width;
        const tx = Math.max(M.l, Math.min(x, w - 4 - tw/2));
        // 收尾点冲到右上角时会正好压住竖排图例（入库金额/出库金额），改画到点的下方
        if(i === s.data.length - 1 && i > 0 && x > w*0.6 && y < M.t + font*3.5){
          vy = Math.min(y + font + 12 + si * (font + 3), h - bRot - 2);
        }
        valLabels.push({x: tx, y: vy, t: fmtNum(v), w: tw});
      });
    });
    // 统一让位：按 x 排序，和上一个标签挤在一起就丢掉（精确值悬停可见）
    ctx.fillStyle = value; ctx.textAlign = 'center';
    let vRight = -1e9;
    valLabels.sort(function(a,b){ return a.x - b.x; });
    valLabels.forEach(function(l){
      if(l.x - l.w/2 < vRight + 6) return;
      ctx.fillText(l.t, l.x, l.y);
      vRight = l.x + l.w/2;
    });
    if(series.length > 1) legend(ctx, w, series.map(s=>({name:s.name, color:s.color||PALETTE[0]})), font, sub, true);
    canvas.onmousemove = e=>{
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX-rect.left;
      let best=null, bd=1e9;
      pts.forEach(p=>{
        const d = Math.abs(p.x-x);
        if(d<bd){ bd=d; best=p; }
      });
      if(best && bd < step*0.6){
        canvas.style.cursor='pointer';
        showTip(e.clientX, e.clientY, '<b>'+esc(best.lb)+'</b> '+esc(best.name)+(window.COLON||'：')+fmtNum(best.v));
      } else { canvas.style.cursor='default'; hideTip(); }
    };
    canvas.onmouseleave = hideTip;
  };

  /* 组合图：柱=数量（左轴）、折线=金额（右轴）。
     入库趋势与出库趋势各自成图后，一张卡里要同时说清「多少件」和「多少钱」。
     两者量纲常差几十倍，塞进同一根轴里柱子会被压成贴地的一条线，所以必须双轴：
     左轴刻度配柱子，右轴刻度用折线色标，图例在顶部自动换行（右上角会被高柱压住）。 */
  window.drawCombo = function(id, labels, bars, lines, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    bars = bars || []; lines = lines || [];
    if(!labels || !labels.length || (!bars.length && !lines.length)){ noData(canvas); return; }
    const totals = labels.map((_, i)=>{
      let s = 0;
      bars.forEach(b=>{ s += Number(b.data[i]) || 0; });
      return s;
    });
    const lineHasData = lines.some(s=>s.data.some(v=>Number(v) > 0));
    if(!totals.some(v=>v > 0) && !lineHasData){ noData(canvas); return; }
    const {ctx, w, h} = setup(canvas);
    const font = autoFont(o, h), text = opt(o,'text'), grid = opt(o,'grid'),
          line = opt(o,'line'), sub = opt(o,'sub'), bg = opt(o,'bg') || '#fff';
    const n = labels.length;
    const items = bars.map((s, i)=>({name: s.name, color: s.color || PALETTE[i % PALETTE.length]}))
      .concat(lines.map((s, i)=>({name: s.name, color: s.color || PALETTE[(bars.length + i) % PALETTE.length]})));
    // ---- 顶部图例（自动换行）----
    const lgFont = Math.max(9, Math.min(font, 11));
    ctx.font = lgFont + 'px "Microsoft YaHei",sans-serif';
    let lx = 10, ly = 13;
    items.forEach(it=>{
      const tw = ctx.measureText(it.name).width + 20;
      if(lx + tw > w - 12 && lx > 10){ lx = 10; ly += lgFont + 7; }
      ctx.fillStyle = it.color; ctx.fillRect(lx, ly - lgFont + 2, 9, 9);
      ctx.fillStyle = sub; ctx.textAlign = 'left';
      ctx.fillText(it.name, lx + 13, ly + 1);
      lx += tw;
    });
    // 左轴（数量）：步长取整，刻度才会是 0/15/30/45 而不是 0/17/33/50
    const barStep = niceStep(Math.max(1, ...(totals.some(v=>v > 0) ? totals : [1])) / 3, false);
    const barMax = barStep * 3;
    // 注意：柱子是堆叠的（多系列时各系列摞在一根柱上），左轴上限必须按柱顶总和取，
    // 取单系列最大值会让堆叠柱冲出绘图区
    const _lmax = Math.max(1, ...(lines.length ? lines.flatMap(s=>s.data.map(v=>Number(v) || 0)) : [1]));
    const lineStep = niceStep(_lmax / 3, !!o && !!o.fineLine);
    const lineMax = lineStep * 3;
    const lf = font, lineH = lf + 6;
    const maxLen = Math.max.apply(null, labels.map(l=>String(l).length));
    const cpl = Math.max(2, Math.floor(((w - 68) / n - 10) / (lf * 0.62)));
    const maxLines = Math.max(1, Math.ceil(maxLen / cpl));
    let bRot = 10 + maxLines * lineH + lf + 12;
    bRot = Math.max(30, Math.min(bRot, Math.floor(h * 0.45)));
    const M = {l: 48, r: lines.length ? 60 : 12, t: ly + 12, b: bRot};
    const pw = w - M.l - M.r, ph = h - M.t - M.b;
    const groupW = pw / n;
    const yFont = Math.max(9, font - 1);
    // 左轴（数量）
    ctx.font = yFont + 'px "Microsoft YaHei",sans-serif';
    for(let i=0;i<=3;i++){
      const v = barMax * i / 3, y = M.t + ph - ph * i / 3;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(w - M.r, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = 'right';
      ctx.fillText(fmtNum(v), M.l - 6, y + yFont * 0.35);
    }
    // 右轴（金额）：不画网格，只标数字，颜色跟折线，避免和左轴混淆
    if(lines.length){
      const rc = lines[0].color || PALETTE[bars.length % PALETTE.length];
      ctx.fillStyle = rc; ctx.textAlign = 'left';
      for(let i=0;i<=3;i++){
        const v = lineMax * i / 3, y = M.t + ph - ph * i / 3;
        ctx.fillText(fmtNum(v), w - M.r + 6, y + yFont * 0.35);
      }
    }
    ctx.strokeStyle = line;
    ctx.beginPath(); ctx.moveTo(M.l, M.t + ph); ctx.lineTo(w - M.r, M.t + ph); ctx.stroke();
    // 横轴标签：日期这类定长串放不下时**隔位抽稀**，不要拆成两行 ——
    // 日周期 14 根柱时每格只有 20 来像素，「10-20」被硬切成「10-2 / 0」比少画几个还难读
    ctx.font = lf + 'px "Microsoft YaHei",sans-serif';
    let maxLblW = 0;
    labels.forEach(lb=>{ maxLblW = Math.max(maxLblW, ctx.measureText(String(lb)).width); });
    const lblStep = Math.max(1, Math.ceil((maxLblW + 8) / groupW));
    const shown = [];
    for(let i = 0; i < n; i += lblStep) shown.push(i);
    // 末点一定要标（看趋势最后落在哪），但放不下就别硬塞第二行 —— 用末点顶掉上一个
    if(n > 1 && shown[shown.length - 1] !== n - 1){
      if((n - 1 - shown[shown.length - 1]) * groupW >= maxLblW + 6) shown.push(n - 1);
      else shown[shown.length - 1] = n - 1;
    }
    shown.forEach(i=>{
      ctx.fillStyle = text; ctx.textAlign = 'center';
      ctx.fillText(String(labels[i]), M.l + groupW * i + groupW / 2, h - bRot + 8 + lf);
    });
    // 柱子：多系列一律堆叠（总览「领用趋势」把 Top5+其他 摞在一根柱上看构成；
    // 单系列堆叠就是普通柱状图，出入库趋势的画法不受影响）
    const barW = Math.min(groupW * 0.5, 46);
    const base = M.t + ph;
    const acc = new Array(n).fill(0);
    bars.forEach((s, si)=>{
      const color = s.color || PALETTE[si % PALETTE.length];
      s.data.forEach((v, i)=>{
        v = Number(v) || 0;
        if(v <= 0) return;
        const bh = (v / barMax) * ph;
        const bx = M.l + groupW * i + (groupW - barW) / 2;
        const by = base - acc[i] - bh;
        ctx.fillStyle = color; ctx.fillRect(bx, by, barW, bh);
        if(acc[i] > 0){   // 段间一条白缝，同色相邻才不会糊成一整块
          ctx.strokeStyle = bg; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + barW, by); ctx.stroke();
        }
        acc[i] += bh;
      });
    });
    // 柱顶合计：堆叠后逐段标值会挤成一团，只在最顶上标总和
    //（单系列时总和就是该系列自身的值，和原来的行为完全一致）
    ctx.fillStyle = sub; ctx.textAlign = 'center';
    ctx.font = Math.max(9, font - 1) + 'px sans-serif';
    for(let i = 0; i < n; i++){
      // 柱子多时（日度 31 根那档）逐根标值会连成一条黑带，与横轴标签同抽稀，
      // 保证「有标签的地方才有数字」，并且末点一定有
      if(totals[i] > 0 && shown.indexOf(i) >= 0){
        // 柱顶贴到顶边时（值刚好等于轴上限）把数字压回绘图区内，否则上半截被裁掉
        const ty = Math.max(M.t + font, base - acc[i] - 3);
        ctx.fillText(fmtNum(totals[i]), M.l + groupW * i + groupW / 2, ty);
      }
    }
    // 折线
    lines.forEach((s, si)=>{
      const color = s.color || PALETTE[(bars.length + si) % PALETTE.length];
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
      s.data.forEach((v, i)=>{
        v = Number(v) || 0;
        const x = M.l + groupW * i + groupW / 2, y = M.t + ph - (v / lineMax) * ph;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.fillStyle = color;
      s.data.forEach((v, i)=>{
        const x = M.l + groupW * i + groupW / 2, y = M.t + ph - ((Number(v) || 0) / lineMax) * ph;
        ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
      });
      // 折线数值：跟横轴同频率抽稀，画在点上方 —— 每点都标会和柱顶数字叠成一团。
      // 线落进柱子范围内时干脆不标：那个位置已经有柱顶数值了，标上去就是两团字糊一起。
      ctx.fillStyle = color; ctx.textAlign = 'center';
      ctx.font = Math.max(9, font - 2) + 'px sans-serif';
      s.data.forEach((v, i)=>{
        if(shown.indexOf(i) < 0) return;
        v = Number(v) || 0;
        if(v <= 0) return;
        const x = M.l + groupW * i + groupW / 2;
        const y = M.t + ph - (v / lineMax) * ph;
        const barTop = M.t + ph - (totals[i] / barMax) * ph;
        if(y > barTop - 14) return;
        // 首尾两个点贴着绘图区边缘，数值写上去会压到左轴刻度/右轴数字，宁可不标
        if(x - M.l < 20 || (w - M.r) - x < 20) return;
        ctx.fillText(fmtNum(v), x, Math.max(M.t + font, y - 4));
      });
    });
    canvas.onmousemove = e=>{
      const rect = canvas.getBoundingClientRect();
      // 按位图/CSS 的实际比例换算点击位置：canvas 被 CSS 拉伸（卡片刚换宽窄）时
      // rect.width 与绘图坐标 w 不相等，直接用 clientX-rect.left 会点偏一格
      const x = (e.clientX - rect.left) * (w / (rect.width || w));
      let idx = Math.floor((x - M.l) / groupW);
      idx = Math.max(0, Math.min(n - 1, idx));
      if(x < M.l - 4 || x > w - M.r + 4){ canvas.style.cursor = 'default'; hideTip(); return; }
      const parts = ['<b>' + esc(labels[idx]) + '</b>'];
      bars.forEach(s=>{ parts.push(esc(s.name) + (window.COLON || '：') + fmtNum(s.data[idx] || 0)); });
      lines.forEach(s=>{ parts.push(esc(s.name) + (window.COLON || '：') + fmtNum(s.data[idx] || 0)); });
      canvas.style.cursor = 'pointer';
      showTip(e.clientX, e.clientY, parts.join('<br>'));
    };
    canvas.onmouseleave = hideTip;
    // 柱子点击：o.onBar(idx, label) —— 领用趋势点某根柱弹柏拉图用。
    // 落在绘图区外（左轴刻度、右轴金额、图例）不触发，避免误点。
    if(o && typeof o.onBar === 'function'){
      canvas.onclick = e=>{
        const rect = canvas.getBoundingClientRect();
        const x = (e.clientX - rect.left) * (w / (rect.width || w));
        if(x < M.l || x > w - M.r) return;
        let idx = Math.floor((x - M.l) / groupW);
        idx = Math.max(0, Math.min(n - 1, idx));
        o.onBar(idx, labels[idx], totals[idx]);
      };
    } else {
      canvas.onclick = null;
    }
  };

  /* 堆叠柱状图：总览「领用趋势」用。
     为什么不用 drawBars：线别/设备/设备类型往往 5~8 个分类，分组并排柱子会细成头发丝；
     而且看这张图要的是「某个时间点里谁占大头」，堆叠面积才是对的编码。
     图例改画在顶部并自动换行 —— 竖排图例放在右上角会被高高的堆叠柱压住。 */
  window.drawStacked = function(id, labels, series, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    if(!labels || !labels.length || !series || !series.length){ noData(canvas); return; }
    const totals = labels.map((_, i)=>series.reduce((a, s)=>a + (s.data[i] || 0), 0));
    if(!totals.some(v=>v > 0)){ noData(canvas); return; }
    const {ctx, w, h} = setup(canvas);
    const font = autoFont(o, h), text = opt(o,'text'), grid = opt(o,'grid'),
          line = opt(o,'line'), sub = opt(o,'sub');
    const n = labels.length;
    const items = series.map((s, i)=>({name: s.name, color: s.color || PALETTE[i % PALETTE.length]}));
    // ---- 顶部图例（自动换行）----
    const lgFont = Math.max(9, Math.min(font, 11));
    ctx.font = lgFont + 'px "Microsoft YaHei",sans-serif';
    const rowsL = [[]];
    let lx = 0;
    items.forEach(it=>{
      const tw = ctx.measureText(it.name).width + 18;
      if(lx + tw > w - 24 && rowsL[rowsL.length - 1].length){ rowsL.push([]); lx = 0; }
      rowsL[rowsL.length - 1].push({name: it.name, color: it.color, w: tw});
      lx += tw;
    });
    const rowH = lgFont + 7;
    let ly = 13;
    rowsL.forEach(row=>{
      let x = 12;
      row.forEach(it=>{
        ctx.fillStyle = it.color;
        ctx.fillRect(x, ly - lgFont + 2, 9, 9);
        ctx.fillStyle = sub; ctx.textAlign = 'left';
        ctx.font = lgFont + 'px "Microsoft YaHei",sans-serif';
        ctx.fillText(it.name, x + 13, ly + 1);
        x += it.w;
      });
      ly += rowH;
    });
    // ---- 绘图区 ----
    const maxVal = niceMax(Math.max(1, ...totals));
    const groupW = (w - 48 - 12) / n;
    const maxLen = Math.max.apply(null, labels.map(l=>String(l).length));
    const lf = Math.max(8, Math.min(font, Math.floor((groupW - 4) / (maxLen * 0.62))));
    const M = {l: 48, r: 12, t: ly + 8, b: lf + 14};
    const pw = w - M.l - M.r, ph = h - M.t - M.b;
    const yFont = Math.max(9, font - 1);
    ctx.font = yFont + 'px "Microsoft YaHei",sans-serif';
    for(let i=0;i<=3;i++){
      const v = maxVal * i / 3, y = M.t + ph - ph * i / 3;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(w - M.r, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = 'right';
      ctx.fillText(fmtNum(v), M.l - 6, y + yFont * 0.35);
    }
    ctx.strokeStyle = line;
    ctx.beginPath(); ctx.moveTo(M.l, M.t + ph); ctx.lineTo(w - M.r, M.t + ph); ctx.stroke();
    const barW = Math.min(groupW * 0.62, 46);
    const base = M.t + ph;
    const rects = [];
    labels.forEach((lb, i)=>{
      const bx = M.l + groupW * i + (groupW - barW) / 2;
      let acc = 0;
      series.forEach((s)=>{
        const v = s.data[i] || 0;
        if(v <= 0) return;
        const sh = (v / maxVal) * ph;
        const y = base - acc - sh;
        ctx.fillStyle = s.color || PALETTE[0];
        ctx.fillRect(bx, y, barW, sh);
        if(acc > 0){   // 段与段之间留一条白缝，否则同色相邻会糊成一整块
          ctx.strokeStyle = opt(o,'bg') || '#fff'; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(bx, y); ctx.lineTo(bx + barW, y); ctx.stroke();
        }
        rects.push({x: bx, y: y, w: barW, h: sh, name: s.name, v: v, i: i, lb: lb});
        acc += sh;
      });
      // 柱顶合计
      if(totals[i] > 0){
        ctx.fillStyle = sub; ctx.textAlign = 'center';
        ctx.font = Math.max(9, font - 1) + 'px sans-serif';
        ctx.fillText(fmtNum(totals[i]), bx + barW / 2, base - acc - 3);
      }
      // 横轴标签
      ctx.fillStyle = text; ctx.textAlign = 'center';
      ctx.font = lf + 'px "Microsoft YaHei",sans-serif';
      ctx.fillText(String(lb), bx + barW / 2, h - 5);
    });
    canvas.onmousemove = e=>{
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      let hit = null;
      rects.forEach(r=>{ if(x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) hit = r; });
      if(hit){
        canvas.style.cursor = 'pointer';
        const tot = totals[hit.i] || 0;
        const pcs = tot ? ' (' + (hit.v / tot * 100).toFixed(1) + '%)' : '';
        showTip(e.clientX, e.clientY, '<b>' + esc(hit.lb) + '</b> ' + esc(hit.name) +
                (window.COLON || '：') + fmtNum(hit.v) + pcs);
      } else { canvas.style.cursor = 'default'; hideTip(); }
    };
    canvas.onmouseleave = hideTip;
  };

  window.drawPie = function(id, data, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    const {ctx, w, h} = setup(canvas);
    const font = opt(o,'font'), text = opt(o,'text'), sub = opt(o,'sub');
    const total = data.reduce((s,d)=>s+d.value, 0);
    if(total<=0){
      ctx.fillStyle=text; ctx.textAlign='center'; ctx.font=font+'px sans-serif';
      ctx.fillText(TR('暂无数据'), w/2, h/2);
      return;
    }
    // 默认色板走「单一主色阶 + 一个暖色点缀」，不是彩虹轮转。
    // 深色大屏会由调用方传 o.colors（每套配色一份，见 app.SCREEN_CHART_PALETTES）。
    const colors = (o && o.colors && o.colors.length) ? o.colors :
      ['#2563eb','#22c55e','#f97316','#8b5cf6','#06b6d4','#ef4444','#eab308','#14b8a6','#f472b6','#64748b'];
    // 扇区之间的缝用**卡片底色**而不是白色：深色大屏上白缝太硬，像贴纸拼的
    const sep = (o && o.sep) ? o.sep : '#ffffff';
    const donut = !!(o && o.donut);
    // 饼图自适应排布：卡片宽而扁 → 饼在左、图例竖排在右；
    // 卡片偏方正或偏高（比如大屏里跨两行的那张）→ 饼在上、图例横排折行放下面。
    // 以前半径写死封顶 100px，卡片一变高就只剩个小饼飘在左上角，右边一大片空。
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';
    // 图例里的名字最多 14 个字：分类名常常是「超长分类超长分类…」这种脏数据，
    // 不截断会一路画到画布外，看起来像图例重影（v1.0.7 实测）。
    const capName = (o && o.nameMax) ? o.nameMax : 999;
    function shortName(s){
      s = (s==null ? '' : String(s));
      return s.length > capName ? s.slice(0, capName - 1) + '…' : s;
    }
    const labels = data.map(function(d){
      const pct = total ? (d.value/total*100).toFixed(1) : '0.0';
      return esc(shortName(d.name)) + '  ' + fmtNum(d.value) + ' (' + pct + '%)';
    });
    // 单个标签比可画宽度还长时截断，避免串到饼上或画到画布外
    function fitText(s, maxW){
      if(ctx.measureText(s).width <= maxW) return s;
      let t = s;
      while(t.length > 1 && ctx.measureText(t + '…').width > maxW) t = t.slice(0, -1);
      return t + '…';
    }
    const lineH = Math.max(16, font + 8);
    const tall = (w / Math.max(h, 1)) < 2.0;
    let cx, cy, R, pieAreaH = h, innerR = 0;
    if(tall){
      const rowsArr = [];
      let cur = [], curW = 0;
      labels.forEach(function(s, i){
        const tw = Math.min(ctx.measureText(s).width, w - 26) + 26;   // 10px 色块 + 间距
        if(cur.length && curW + tw > w){ rowsArr.push({items:cur, w:curW}); cur = []; curW = 0; }
        cur.push({s:s, i:i, tw:tw}); curW += tw + 14;
      });
      if(cur.length) rowsArr.push({items:cur, w:curW});
      const legendH = rowsArr.length * lineH + 4;
      pieAreaH = Math.max(60, h - legendH - 4);
      cx = w / 2; cy = pieAreaH / 2;
      R = Math.max(18, Math.min(pieAreaH/2 - 8, w/2 - 16));
      var drawBottomLegend = function(){
        let y = pieAreaH + lineH;
        rowsArr.forEach(function(r){
          let x = Math.max(2, (w - (r.w - 14)) / 2);
          r.items.forEach(function(it){
            // 最后一个图例项可能超出右边界：按剩余宽度再截一次
            const avail = w - x - 16;
            if(avail < 24) return;
            ctx.fillStyle = colors[it.i % colors.length];
            ctx.fillRect(x, y - font + 2, 10, 10);
            ctx.fillStyle = sub; ctx.textAlign = 'left';
            ctx.fillText(fitText(it.s, avail), x + 14, y);
            x += it.tw + 14;
          });
          y += lineH;
        });
      };
    } else {
      const pieW = Math.max(90, w * 0.46);
      cx = pieW / 2; cy = h / 2;
      R = Math.max(18, Math.min(h/2 - 16, pieW/2 - 12));
      var drawRightLegend = function(){
        const lx = pieW + 12, maxW = w - lx - 6;
        const lw = Math.max(16, font + 8);
        let ly = Math.max(font + 6, (h - data.length * lw)/2 + font*0.8);
        data.forEach(function(d, i){
          ctx.fillStyle = colors[i % colors.length]; ctx.fillRect(lx, ly - font + 2, 10, 10);
          ctx.fillStyle = sub; ctx.textAlign = 'left';
          ctx.fillText(fitText(labels[i], maxW), lx + 14, ly);
          ly += lw;
        });
      };
    }
    if(donut) innerR = R * (o && o.donutRatio ? o.donutRatio : 0.58);
    let a0 = -Math.PI/2;
    const slices = [];
    data.forEach((d, i)=>{
      const a1 = a0 + (d.value/total)*Math.PI*2;
      ctx.beginPath(); ctx.moveTo(cx,cy); ctx.arc(cx,cy,R,a0,a1); ctx.closePath();
      ctx.fillStyle = colors[i%colors.length]; ctx.fill();
      ctx.strokeStyle = sep; ctx.lineWidth = 2; ctx.stroke();
      slices.push({a0,a1,d,color:colors[i%colors.length]});
      a0 = a1;
    });
    // 圆环：饼心挖一个卡片底色的圆（不是透明，避免残留上一次的图形）
    if(donut && innerR > 4){
      ctx.beginPath(); ctx.arc(cx, cy, innerR, 0, Math.PI*2);
      ctx.fillStyle = sep; ctx.fill();
      // 环心文字：合计值 + 一句小标签，让环心不空着
      const cv = (o && o.centerValue != null) ? o.centerValue : fmtNum(total);
      const cs = (o && o.centerLabel) || '';
      const vf = Math.max(12, Math.min(innerR * 0.44, font * 2.1));
      ctx.textAlign = 'center';
      ctx.fillStyle = (o && o.centerNum) ? o.centerNum : text;
      ctx.font = '600 ' + Math.round(vf) + 'px "Microsoft YaHei",sans-serif';
      ctx.fillText(String(cv), cx, cs ? cy + vf * 0.12 : cy + vf * 0.35);
      if(cs){
        ctx.fillStyle = sub;
        ctx.font = Math.max(9, Math.round(vf * 0.42)) + 'px "Microsoft YaHei",sans-serif';
        ctx.fillText(cs, cx, cy + vf * 0.92);
      }
      ctx.textAlign = 'left';
    }
    if(tall) drawBottomLegend(); else drawRightLegend();
    canvas.onmousemove = e=>{
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX-rect.left, y = e.clientY-rect.top;
      const dx=x-cx, dy=y-cy;
      const dist = Math.hypot(dx,dy);
      if(dist<=R && dist>=innerR){
        let ang = Math.atan2(dy,dx);
        if(ang < -Math.PI/2) ang += Math.PI*2;
        const hit = slices.find(s=>ang>=s.a0 && ang<s.a1) || slices[slices.length-1];
        if(hit){
          canvas.style.cursor='pointer';
          showTip(e.clientX, e.clientY, '<b>'+esc(hit.d.name)+'</b>'+(window.COLON||'：')+fmtNum(hit.d.value)+' '+TR('元')+' ('+
            (hit.d.value/total*100).toFixed(1)+'%)');
        }
      } else { canvas.style.cursor='default'; hideTip(); }
    };
    canvas.onmouseleave = hideTip;
  };

  /* 柏拉图（Pareto）：柱子按数值从大到小排，折线是累计占比（右轴 0~100%），
     再画一条 80% 虚线做「前几项就占了八成」的分界。领用趋势点柱子后弹的就是它。
     rows 必须已经按 value 降序传入（排序在后端做，前端切维度时不再重排）。 */
  window.drawPareto = function(id, rows, o){
    const canvas = document.getElementById(id);
    if(!canvas) return;
    rows = (rows || []).filter(r=>r && Number(r.value) > 0);
    if(!rows.length){ noData(canvas); return; }
    const {ctx, w, h} = setup(canvas);
    const font = autoFont(o, h), text = opt(o,'text'), grid = opt(o,'grid'),
          line = opt(o,'line'), sub = opt(o,'sub');
    const colors = (o && o.colors && o.colors.length) ? o.colors :
      ['#2563eb','#22c55e','#f97316','#8b5cf6','#06b6d4','#ef4444','#eab308','#14b8a6','#f472b6','#64748b'];
    const n = rows.length;
    const total = rows.reduce((a,r)=>a + (Number(r.value) || 0), 0) || 1;
    let run = 0;
    const cum = rows.map(r=>{ run += Number(r.value) || 0; return run / total * 100; });
    const vmaxRaw = Math.max.apply(null, rows.map(r=>Number(r.value) || 0));
    const step = niceStep(Math.max(1, vmaxRaw) / 3, false);
    const vmax = Math.max(step * 3, vmaxRaw);
    // 顶部图例：柱=数值、线=累计占比
    const lgFont = Math.max(9, Math.min(font, 11));
    ctx.font = lgFont + 'px "Microsoft YaHei",sans-serif';
    const legendItems = [{name: (o && o.barName) || TR('数量'), color: colors[0]},
                         {name: TR('累计占比'), color: '#111827'}];
    let lx = 10, ly = 13;
    legendItems.forEach(it=>{
      const tw = ctx.measureText(it.name).width + 20;
      if(lx + tw > w - 12 && lx > 10){ lx = 10; ly += lgFont + 7; }
      ctx.fillStyle = it.color; ctx.fillRect(lx, ly - lgFont + 2, 9, 9);
      ctx.fillStyle = sub; ctx.textAlign = 'left';
      ctx.fillText(it.name, lx + 13, ly + 1);
      lx += tw;
    });
    // 横轴标签太长就截断（完整名字走悬停提示）：十几个物料编码全写出来会连成一片
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';
    const M = {l: Math.max(34, autoLeftM(ctx, vmax, font)), r: 46, t: ly + 14, b: 30};
    const pw = Math.max(40, w - M.l - M.r), ph = Math.max(40, h - M.t - M.b);
    const groupW = pw / n;
    const barW = Math.max(3, Math.min(groupW * 0.62, 54));
    const base = M.t + ph;
    const yFont = Math.max(9, font - 1);
    // 左轴（数值）
    ctx.font = yFont + 'px "Microsoft YaHei",sans-serif';
    for(let i=0;i<=3;i++){
      const y = base - ph * i / 3;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(M.l, y); ctx.lineTo(w - M.r, y); ctx.stroke();
      ctx.fillStyle = text; ctx.textAlign = 'right';
      ctx.fillText(fmtNum(vmax * i / 3), M.l - 6, y + yFont * 0.35);
    }
    // 右轴（累计占比 %）：只标刻度不画网格，避免和左轴混
    ctx.fillStyle = '#111827'; ctx.textAlign = 'left';
    for(let i=0;i<=4;i++){
      const y = base - ph * i / 4;
      ctx.fillText(Math.round(i * 25) + '%', w - M.r + 6, y + yFont * 0.35);
    }
    // 柱子
    rows.forEach((r, i)=>{
      const v = Number(r.value) || 0;
      const bh = Math.max(1, (v / vmax) * ph);
      const bx = M.l + groupW * i + (groupW - barW) / 2;
      ctx.fillStyle = colors[i % colors.length];
      ctx.fillRect(bx, base - bh, barW, bh);
    });
    // 柱顶数值：柱子太密（<22px 一格）就不标，标了也是一条黑带
    if(groupW >= 22){
      ctx.fillStyle = sub; ctx.textAlign = 'center';
      ctx.font = Math.max(9, font - 1) + 'px sans-serif';
      rows.forEach((r, i)=>{
        const v = Number(r.value) || 0;
        if(v <= 0) return;
        const y = base - (v / vmax) * ph;
        const x = M.l + groupW * i + groupW / 2;
        // 柱子顶到绘图区上沿（值≈轴上限）时数字没地方放，改成写在柱子里的白字；
        // 否则压到图例上，或者被裁掉半截
        if(y < M.t + 12){
          ctx.fillStyle = '#fff';
          ctx.fillText(fmtNum(v), x, Math.min(base - 6, y + 15));
          ctx.fillStyle = sub;
        } else {
          ctx.fillText(fmtNum(v), x, y - 5);
        }
      });
    }
    // 80% 参考线（虚线 + 右侧小标签）
    const y80 = base - ph * 0.8;
    ctx.save();
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(M.l, y80); ctx.lineTo(w - M.r, y80); ctx.stroke();
    ctx.restore();
    // 累计占比折线
    ctx.strokeStyle = '#111827'; ctx.lineWidth = 2; ctx.beginPath();
    cum.forEach((p, i)=>{
      const x = M.l + groupW * i + groupW / 2, y = base - ph * p / 100;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = '#111827';
    cum.forEach((p, i)=>{
      const x = M.l + groupW * i + groupW / 2, y = base - ph * p / 100;
      ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
    });
    // 累计百分比数值：格子太窄就隔位标，避免数字互相压
    const labStep = Math.max(1, Math.ceil(26 / Math.max(groupW, 1)));
    ctx.font = Math.max(9, font - 2) + 'px sans-serif'; ctx.textAlign = 'center';
    cum.forEach((p, i)=>{
      if(i % labStep !== 0 && i !== n - 1) return;
      const x = M.l + groupW * i + groupW / 2, y = base - ph * p / 100;
      const txt = Math.round(p) + '%';
      // 折线点贴着柱顶时（第一根柱最常见：它占大头，累计 90% 离柱顶只差十几像素），
      // 数字会正好压在柱顶数值上。改成往右挪一点、垫一块半透明白底，
      // 既躲开柱顶数值，压在色块上也还看得清。
      const barTop = base - ph * ((Number(rows[i].value) || 0) / vmax);
      if(Math.abs(y - barTop) < 18){
        const tw = ctx.measureText(txt).width + 8;
        ctx.fillStyle = 'rgba(255,255,255,.88)';
        ctx.fillRect(x + 6, Math.max(M.t - 2, y - 17), tw, 14);
        ctx.fillStyle = '#111827'; ctx.textAlign = 'left';
        ctx.fillText(txt, x + 10, Math.max(M.t + 9, y - 6));
        ctx.textAlign = 'center';
        return;
      }
      ctx.fillStyle = '#111827';
      ctx.fillText(txt, x, Math.max(M.t + font - 2, y - 7));
    });
    // 横轴标签
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';
    const maxW = Math.max(20, groupW - 4);
    ctx.fillStyle = text;
    rows.forEach((r, i)=>{
      let s = String(r.name == null ? '' : r.name);
      if(ctx.measureText(s).width > maxW){
        while(s.length > 1 && ctx.measureText(s + '…').width > maxW) s = s.slice(0, -1);
        s += '…';
      }
      ctx.textAlign = 'center';
      ctx.fillText(s, M.l + groupW * i + groupW / 2, base + 16);
    });
    // 悬停：完整分类名 + 数值 + 累计占比
    canvas.onmousemove = e=>{
      const rect = canvas.getBoundingClientRect();
      const x = (e.clientX - rect.left) * (w / (rect.width || w));
      if(x < M.l || x > w - M.r){ canvas.style.cursor = 'default'; hideTip(); return; }
      let idx = Math.floor((x - M.l) / groupW);
      idx = Math.max(0, Math.min(n - 1, idx));
      canvas.style.cursor = 'pointer';
      showTip(e.clientX, e.clientY,
        '<b>' + esc(rows[idx].name) + '</b><br>' +
        esc((o && o.barName) || TR('数量')) + (window.COLON || '：') + fmtNum(rows[idx].value) +
        '<br>' + TR('累计占比') + (window.COLON || '：') + cum[idx].toFixed(1) + '%');
    };
    canvas.onmouseleave = hideTip;
    canvas.onclick = null;
  };

  function legend(ctx, w, items, font, color, vert){
    // 多语言下标签长度差异大：默认横排自动缩字号防重叠；
    // vert=true（出入库趋势图）固定竖排右上角，任何语言标签永不重叠
    ctx.font = font + 'px "Microsoft YaHei",sans-serif';
    if(vert && items.length > 1){
      ctx.textAlign = 'right';
      let y = 16;
      for(let i=0;i<items.length;i++){
        ctx.fillStyle = items[i].color || PALETTE[i % PALETTE.length];
        ctx.fillRect(w-24, y-9, 9, 9);
        ctx.fillStyle = color;
        ctx.fillText(items[i].name, w-30, y);
        y += Math.max(16, font + 8);
      }
      return;
    }
    let gap = Math.max(6, font);
    let need = 20;
    for(let i=0;i<items.length;i++) need += ctx.measureText(items[i].name).width + gap;
    if(need > w && items.length){
      font = Math.max(8, Math.floor(font * (w - 24) / need));
      gap = Math.max(5, Math.floor(font * 0.8));
      ctx.font = font + 'px "Microsoft YaHei",sans-serif';
      need = 20;
      for(let i=0;i<items.length;i++) need += ctx.measureText(items[i].name).width + gap;
    }
    if(need > w && items.length > 1){
      // 竖排兜底：从右上角向下每项一行
      ctx.textAlign = 'right';
      let y = 16;
      for(let i=0;i<items.length;i++){
        ctx.fillStyle = items[i].color || PALETTE[i % PALETTE.length];
        ctx.fillRect(w-24, y-9, 9, 9);
        ctx.fillStyle = color;
        ctx.fillText(items[i].name, w-30, y);
        y += Math.max(16, font + 8);
      }
      return;
    }
    let x = w - 20;
    ctx.textAlign = 'right';
    for(let i=items.length-1;i>=0;i--){
      const t = items[i].name;
      const tw = ctx.measureText(t).width;
      x -= tw;
      ctx.fillStyle = items[i].color || PALETTE[i % PALETTE.length];
      ctx.fillRect(x-10, 8, 9, 9);
      ctx.fillStyle = color;
      ctx.fillText(t, x-14, 17);
      x -= gap;
    }
  }
  /* 数字缩写：按语言档案的进位体系来，而不是把「万」这个字换掉。
     中文 50000 → 5万；英文 50000 → 50K。
     只翻后缀的话，5万 会变成 "5 10K"（数值差一个数量级）。 */
  function trimZeros(s){
    return s.replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
  }
  function fmtNum(v){
    v = Number(v || 0);
    const a = Math.abs(v);
    if (window.NUM_SCALE === 'wan') {
      if (a >= 1e8) return trimZeros((v/1e8).toFixed(a >= 1e9 ? 0 : 1)) + TR('亿');
      if (a >= 1e4) return trimZeros((v/1e4).toFixed(a >= 1e5 ? 0 : 1)) + TR('万');
    } else {
      if (a >= 1e9) return trimZeros((v/1e9).toFixed(1)) + 'B';
      if (a >= 1e6) return trimZeros((v/1e6).toFixed(1)) + 'M';
      if (a >= 1e4) return trimZeros((v/1e3).toFixed(0)) + 'K';
    }
    return Math.round(v).toLocaleString(window.LOCALE || 'zh-CN');
  }
  function esc(s){ const d=document.createElement('div'); d.textContent=s==null?'':String(s); return d.innerHTML; }
})();
