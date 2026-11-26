/*
 * SPMS 备件辅料仓管系统 (Spare Parts Management System)
 * 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
 * 邮箱：510658927@qq.com  QQ：510658927
 * Gitee：https://gitee.com/rookie-uncle
 * GitHub：https://github.com/Colton1982
 * Copyright (c) 2026 rookie-uncle. All rights reserved.
 * 开源协议：Apache-2.0
 */
/* 自定义表头：类型不是「文本」时，字符数框变灰且不许录入。
   为什么不 disable：disable 的输入框不参与表单提交，切回「文本」时值就丢了。
   用 readonly + 样式，值照常提交，切回文本自动恢复可编辑。 */
(function () {
  function sync(sel) {
    var row = sel.closest ? sel.closest('tr') : null;
    if (!row) return;
    var clip = row.querySelector('.ef-clip');
    if (!clip) return;
    var off = sel.value !== 'text';
    if (clip.readOnly !== off) clip.readOnly = off;
    if (off) clip.classList.add('is-off');
    else clip.classList.remove('is-off');
  }
  function bind() {
    var list = document.querySelectorAll('.ef-table .h-type');
    for (var i = 0; i < list.length; i++) {
      sync(list[i]);
      list[i].addEventListener('change', (function (s) {
        return function () { sync(s); };
      })(list[i]));
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
