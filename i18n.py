# -*- coding: utf-8 -*-
# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""
i18n.py —— 兼容适配层（v2）

历史包袱说明
------------
旧版 i18n.py 里有三套词条存储（内联 _TR / i18n/*.json 中文键 / zh.json 符号键）
和一条四段式查找链，加一门语言要同时改三处，所以印尼语一直补不齐。

从 v2 起，词条的**唯一真源**是 locale/ 目录：
    locale/languages.json        语言注册表（新增语言只改这里）
    locale/catalog/<lang>.json   该语言的词条（key 是稳定 ID）
    locale/legacy_map.json       ID 台账（中文原文 → ID，兼容旧调用）

本文件只剩「适配层」职责：把老的 i18n.t / i18n.LANGUAGES / i18n.tr_json
签名映射到 i18n2 引擎，让 app.py、模板、旧脚本一行不改就能跑在新内核上。
"""
from __future__ import annotations

import os

import i18n2
from i18n2 import (current_lang, fmt_date, fmt_money, fmt_num, label, lang_scope,
                   locale_tag, mk, msg, num_scale, render_msg, set_lang, text_dir, tn)
from i18n2 import install_flask, install_jinja
from i18n2 import LANG_SRC, LANG_SRC_VER
# 币种：与语言正交，由 sys_settings.currency 决定（见 app._sync_currency）
from i18n2 import (cur_label, currency_list, currency_meta, get_currency,
                    set_currency, set_custom_currency, custom_currency,
                    normalize_custom, iso_lang_list,
                    looks_chinese_authored, term_known)
import i18n2 as _i18n2

_HERE = os.path.dirname(os.path.abspath(__file__))
i18n2.init(os.path.join(_HERE, "locale"))

# 语言注册表（旧代码按 dict 用：for code, name in LANGUAGES.items()）
#
# 必须是「活视图」而不是快照：newlang.py 在进程运行中往 languages.json 加语言，
# 如果这里是 import 时拷贝的 dict，列表页仍然只显示老语言，选择器里根本选不到新语言。
class _LangMap(dict):
    """每次读取前与注册表同步一次的 dict。"""

    def _sync(self):
        data = i18n2.languages()
        # 注意用 dict.* 直接取，避免触发本类的 __len__/get 造成递归
        same = dict.__len__(self) == len(data) and all(
            dict.get(self, k) == v for k, v in data.items())
        if not same:
            self.clear()
            dict.update(self, data)
        return self

    def __len__(self):
        return dict.__len__(self._sync())

    def __iter__(self):
        # 必须用 dict.__iter__：内置 iter(self) 会再次进入本方法，导致无限递归
        self._sync()
        return dict.__iter__(self)

    def __contains__(self, k):
        return dict.__contains__(self._sync(), k)

    def __getitem__(self, k):
        return dict.__getitem__(self._sync(), k)

    def get(self, k, default=None):
        return dict.get(self._sync(), k, default)

    def keys(self):
        return dict.keys(self._sync())

    def values(self):
        return dict.values(self._sync())

    def items(self):
        return dict.items(self._sync())

    def __repr__(self):
        return dict.__repr__(self._sync())


LANGUAGES = _LangMap(i18n2.languages())

# 取词入口
t = i18n2.t
tr = i18n2.t


def tr_json(lang=None, page=None):
    """前端字典。传 page 只给该页用到的词条（分片），否则给全量。"""
    return i18n2.tr_json(lang, page)


def languages():
    return LANGUAGES


def reload():
    """重载语言包（不重启进程），改完译文点一下即可生效。"""
    i18n2.reload()
    global LANGUAGES
    LANGUAGES = i18n2.languages()
    return LANGUAGES


def missing_report():
    return i18n2.missing_report()
