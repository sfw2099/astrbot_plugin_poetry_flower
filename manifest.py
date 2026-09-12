# -*- coding: utf-8 -*-
"""飞花令插件的成就/道具清单（启动时向秋烨注册）。

成就 id 与猜诗句插件一致（同一套诗词成就体系，由猜诗句/对垒解锁，此处仅注册道具）。
"""

ITEMS = {
    "文字狱": {"desc": "纵横飞花令专用：清除场上所有指定汉字及其周围一圈（例：/诗词道具 文字狱 春）"},
    "红杏出墙": {"desc": "纵横飞花令专用：你的下一句诗句可以无视棋盘边界（超出部分不显示）"},
}

# 升级制成就名（store_bridge 引用；与猜诗句插件一致）
CLOSER_LEVELS = [
    (10, "色彩收尾人"),
    (7, "四阶收尾人"),
    (5, "三阶收尾人"),
    (3, "二阶收尾人"),
    (1, "一阶收尾人"),
]


def closer_level_name(progress):
    if progress <= 0:
        return "收尾人"
    for thr, name in CLOSER_LEVELS:
        if progress >= thr:
            return name
    return "收尾人"


def duel_streak_name(n):
    _CN = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    if n <= 0:
        return "连胜未开始"
    if n == 1:
        return "一破·卧龙出山"
    if n == 2:
        return "双连·一战成名"
    if n == 3:
        return "三连·举世皆惊"
    if n == 4:
        return "四连·天下无敌"
    if n >= 5:
        prefix = _CN[n] if n <= 10 else str(n)
        return f"{prefix}连·诛天灭地"
    return "一破·卧龙出山"


def hazard_tier_name(n):
    _CN = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
    if n <= 0:
        return "未历劫难"
    if n >= 9:
        return "九重天"
    return f"{_CN[n]}重天"


ACHIEVEMENTS = {}
