# -*- coding: utf-8 -*-
"""纵横飞花令插件：棋盘拼字飞花令 + 字数池规则 + 存档体系 + 专属道具（文字狱/红杏出墙）。

数据架构：
- 诗词库/个人诗句 → 诗词底座（astrbot_plugin_poetry_base）
- 成就/道具/使用记录 → 秋烨枢纽（astrbot_plugin_qiuye）
- 存档 → 本插件数据目录 saves/
"""

import asyncio
import os
import re
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register, StarTools

from .base_engine import BOT_ID, BOT_NAME
from .engine import CrosswordEngine
from .manifest import ITEMS
from . import links
from .store_bridge import StoreBridge


@register("astrbot_plugin_poetry_flower", "ALin", "纵横飞花令", "1.0.0")
class PoetryFlowerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_flower")
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = str(self.plugin_data_dir)
        self.saves_dir = self.plugin_data_dir / 'saves'
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self.pm = StoreBridge(context)
        self.active_games = {}   # session_id -> engine
        self.timeout_tasks = {}
        self.crossword_timeout = self.config.get("crossword_timeout", 90)
        ok = links.hub_register_manifest(context)
        logger.info(f"[poetry_flower] 纵横飞花令插件已加载。数据目录: {self.data_dir}，秋烨注册{'成功' if ok else '失败(秋烨未就绪)'}")

    # ==================== 基础设施 ====================

    def _uid_name(self, uid):
        return self.pm._uid_name(uid)

    def _achieve_msg(self, uid, ach_id):
        from .manifest import ACHIEVEMENTS
        name = ACHIEVEMENTS.get(ach_id, (ach_id, ""))[0]
        uname = self._uid_name(uid)
        return f"🏆 {uname} 达成成就「{name}」！"

    def _base_ready_msg(self):
        return "⏳ 诗词底座插件未安装或数据库未就绪，请先安装 astrbot_plugin_poetry_base 并发送 /安装数据库"

    def _base_ready(self) -> bool:
        return links.base_db_ready(self.context)

    def _roll_draw(self, uid, uname):
        """统一抽道具掷骰：基础 10% + draw_bonus。返回 (命中?, 提示文本, 成就提示)。"""
        import random as _r
        bonus = self.pm.get_draw_bonus(uid, uname)
        rate = 10 + bonus
        if _r.randint(1, 100) <= rate:
            self.pm.reset_draw_bonus(uid, uname)
            item = _r.choice(list(ITEMS.keys()))
            self.pm.add_item(uid, item, 1, uname)
            return True, f"🎁 抽中了道具【{item}】！本次概率 {rate}%（保底已重置）。", ""
        self.pm.add_draw_bonus(uid, 10, uname)
        new_rate = min(10 + self.pm.get_draw_bonus(uid, uname), 100)
        ach = ""
        if new_rate >= 100 and self.pm.unlock_achievement(uid, "unlucky", uname):
            ach = f"🏆 {uname} 达成成就「真有这么倒霉的人啊？」！"
        return False, f"未抽中（本次 {rate}%），下次概率提升至 {new_rate}%。", ach

    # ==================== 存档管理 ====================

    def get_saves(self, session_id):
        saves = []
        if not os.path.exists(str(self.saves_dir)):
            return saves
        for f in os.listdir(str(self.saves_dir)):
            if f.startswith(f"game_{session_id}_") and f.endswith(".json"):
                path = os.path.join(str(self.saves_dir), f)
                try:
                    with open(path, 'r', encoding='utf-8') as file:
                        state = json.load(file)
                    saves.append({
                        "filename": f,
                        "path": path,
                        "type": state.get("game_type", "未知"),
                        "start_time": state.get("start_time", "未知 (旧版存档)"),
                        "turn_count": state.get("turn_count", 0),
                        "mtime": os.path.getmtime(path),
                    })
                except Exception:
                    pass
        saves.sort(key=lambda x: x["mtime"], reverse=True)
        return saves

    # ==================== 建局 ====================

    @filter.command("纵横飞花令")
    async def start_crossword(self, event: AstrMessageEvent, width: int = 21, height: int = 21):
        if not self._base_ready():
            yield event.plain_result(self._base_ready_msg())
            return
        if not (8 <= width <= 40) or not (8 <= height <= 40):
            yield event.plain_result("📐 棋盘宽和高必须在 8 到 40 之间！")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前群聊已有游戏正在进行！请先【结束游戏】")
            return
        # 字数池：主字数 {5,7} 随机 1 种 + {4,6,8,9,10} 随机 2 种；开局句固定用主字数
        import random as _r
        main_len = _r.choice([5, 7])
        extra_lens = _r.sample([4, 6, 8, 9, 10], 2)
        allowed_lens = sorted(set([main_len] + extra_lens))
        start_verse = None
        try:
            rows = links.base_get_random_verse(self.context, main_len, main_len, target_count=10, max_scan=300)
            if rows:
                start_verse = _r.choice(rows)[0]
        except Exception:
            pass
        if not start_verse:
            start_verse = _r.choice(["春江潮水连海平", "海上明月共潮生", "黄河之水天上来", "人生得意须尽欢"]) if main_len == 7 else _r.choice(["春眠不觉晓", "好雨知时节", "红豆生南国", "君自故乡来"])
        engine = CrosswordEngine(session_id, str(self.saves_dir), width=width, height=height,
                                 timeout_seconds=self.crossword_timeout, start_verse=start_verse,
                                 allowed_lens=allowed_lens)
        self.active_games[session_id] = engine
        if session_id in self.timeout_tasks:
            self.timeout_tasks[session_id].cancel()
        self.timeout_tasks[session_id] = asyncio.create_task(self._active_timeout_monitor(session_id, event.unified_msg_origin))
        allowed = engine.allowed_lens()
        start_verse_info = engine.state["history"][0] if engine.state["history"] else "随机开局"
        yield event.plain_result(
            f"🌟 【纵横飞花令】已建立新对局！({width}x{height}棋盘，限时{self.crossword_timeout}秒)\n"
            f"📏 本局允许字数：{'、'.join(str(x) for x in allowed)} 字\n"
            f"系统已随机落下首句：{start_verse_info}\n"
            f"发送【加入】参与；发送「cc 诗句」落子（需含棋盘上已有的字）。"
        )
        yield event.image_result(engine.render_image())

    @filter.command("结束游戏")
    async def stop_game(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            engine = self.active_games.pop(session_id)
            yield event.plain_result("⏹️ 游戏已结束。最后战果：\n" + engine.generate_text_report())
        else:
            yield event.plain_result("当前没有正在进行的游戏。")

    @filter.command("生成战报")
    async def generate_report(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if not engine:
            yield event.plain_result("当前没有进行中的游戏。如果要生成旧战报，请先【恢复游戏】。")
            return
        yield event.plain_result(engine.generate_text_report())
        if hasattr(engine, "render_image"):
            yield event.image_result(engine.render_image())

    @filter.command("恢复游戏")
    async def load_game(self, event: AstrMessageEvent, arg: str = ""):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.active_games:
            yield event.plain_result("当前已有进行中的游戏，请先【结束游戏】。")
            return
        saves = self.get_saves(session_id)
        if not saves:
            yield event.plain_result("未找到该群的任何游戏存档。")
            return
        if not arg or not arg.isdigit():
            msg = [f"📂 发现 {len(saves)} 个存档，请发送 /恢复游戏 [序号] 来选择：", "-" * 15]
            for i, s in enumerate(saves, 1):
                msg.append(f"[{i}] 纵横飞花令 | 建于: {s['start_time']} | 进度: {s['turn_count']}回合")
            yield event.plain_result("\n".join(msg))
            return
        index = int(arg)
        if index < 1 or index > len(saves):
            yield event.plain_result("❌ 无效的存档序号。")
            return
        target_save = saves[index - 1]
        filename = target_save["filename"]
        engine = CrosswordEngine(session_id, str(self.saves_dir), save_filename=filename)
        try:
            if engine.load_state():
                # 恢复棋盘尺寸
                custom = engine.state.get("custom_data", {})
                grid = custom.get("grid")
                if grid:
                    engine.WIDTH = len(grid[0]) if grid else 21
                    engine.HEIGHT = len(grid)
                    engine.BOARD_W_PX = engine.WIDTH * engine.CELL_SIZE
                    engine.BOARD_H_PX = engine.HEIGHT * engine.CELL_SIZE
                self.active_games[session_id] = engine
                if session_id in self.timeout_tasks:
                    self.timeout_tasks[session_id].cancel()
                self.timeout_tasks[session_id] = asyncio.create_task(self._active_timeout_monitor(session_id, event.unified_msg_origin))
                yield event.plain_result(f"💾 存档 [{index}] 恢复成功！游戏继续。")
                yield event.image_result(engine.render_image())
            else:
                yield event.plain_result("❌ 存档文件读取失败。")
        except Exception as e:
            yield event.plain_result(f"❌ 恢复失败: {e}")

    @filter.command("删除存档")
    async def delete_save(self, event: AstrMessageEvent, arg: str = ""):
        session_id = str(event.get_group_id() or event.get_session_id())
        saves = self.get_saves(session_id)
        if not saves:
            yield event.plain_result("未找到该群的任何游戏存档。")
            return
        if not arg or not arg.isdigit():
            msg = [f"🗑 发现 {len(saves)} 个存档，请发送 /删除存档 [序号] 来永久删除：", "-" * 15]
            for i, s in enumerate(saves, 1):
                msg.append(f"[{i}] 纵横飞花令 | 建于: {s['start_time']} | 进度: {s['turn_count']}回合")
            yield event.plain_result("\n".join(msg))
            return
        index = int(arg)
        if index < 1 or index > len(saves):
            yield event.plain_result("❌ 无效的存档序号。")
            return
        target_save = saves[index - 1]
        try:
            os.remove(target_save["path"])
            yield event.plain_result(f"🗑 存档 [{index}] 已成功删除！")
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    # ==================== 道具（纵横专属） ====================

    @filter.command("诗词道具")
    async def use_item(self, event: AstrMessageEvent, item: str = "", n: str = ""):
        """使用纵横飞花令道具：/诗词道具 文字狱 汉字 | /诗词道具 红杏出墙"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        item = (item or "").strip()
        if item not in ITEMS:
            # 非本插件道具：提示归属
            if item:
                yield event.plain_result(f"道具【{item}】不属于纵横飞花令（本指令仅支持：{'、'.join(ITEMS.keys())}）。猜诗句道具请在猜诗句游戏中使用 /诗词道具。")
            else:
                yield event.plain_result(f"用法：/诗词道具 {'｜'.join(ITEMS.keys())}")
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if engine is None:
            yield event.plain_result("当前群聊没有进行中的纵横飞花令。")
            return
        if self.pm.item_count(uid, item, uname) <= 0:
            yield event.plain_result(f"道具【{item}】数量不足。")
        else:
            self.pm.consume_item(uid, item, 1, uname)
            if item == "文字狱":
                raw = str(event.get_message_str() or "").strip()
                tail = re.sub(r"^[/／]?\s*诗词道具\s*", "", raw, flags=re.IGNORECASE)
                tail = re.sub(r"^文字狱\s*", "", tail).strip()
                resp = engine.step("item_wz", uid, uname, tail)
            elif item == "红杏出墙":
                resp = engine.step("item_hx", uid, uname, "")
            else:
                resp = {"status": "error", "msg": "未知道具。"}
            yield event.plain_result(resp.get("msg", ""))
            if resp.get("image"):
                yield event.image_result(resp["image"])
            # 文字狱可能清空全场导致无落位
            if item == "文字狱" and resp.get("status") == "success" and not engine.has_any_placement():
                self.active_games.pop(session_id, None)
                yield event.plain_result("⛓ 文字狱清场后棋盘已无可落位，游戏自动结束！\n" + engine.generate_text_report())

    # ==================== 消息处理 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_recv_msg(self, event: AstrMessageEvent):
        msg_raw = event.message_str.strip()
        if not msg_raw or msg_raw.startswith(("/", "！", "!")):
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.active_games.get(session_id)
        if engine is None:
            return
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        players = engine.state["players"]
        is_player = any(p['id'] == uid for p in players)
        custom = engine.state.get("custom_data", {})
        pending_for_me = custom.get("pending_options") and custom.get("pending_player_id") == uid

        # 数字 → 抉择（仅挂起者）
        if pending_for_me and msg_raw.isdigit():
            links.hub_record_plugin_use(self.context, uid, uname)
            resp = engine.step("choice", uid, uname, msg_raw)
            async for m in self._emit(event, engine, session_id, resp, uid, uname):
                yield m
            return

        # cc 诗句 → 落子
        if msg_raw.startswith("cc"):
            if not is_player:
                return  # 非玩家静默
            clean = re.sub(r"^cc\s*", "", msg_raw).strip()
            hanzi = re.sub(r"[^\u4e00-\u9fff]", "", clean)
            if not hanzi:
                return
            # 库校验 + 出处
            if not self._base_ready():
                yield event.plain_result(self._base_ready_msg())
                return
            meta = links.base_check_exact_poetry(self.context, hanzi)
            if not meta:
                yield event.plain_result(f"「{hanzi}」不在诗词库中，请输入库中完整诗句。")
                return
            links.hub_record_plugin_use(self.context, uid, uname)
            resp = engine.step("play", uid, uname, hanzi, verse_meta=(meta[0], meta[1]))
            async for m in self._emit(event, engine, session_id, resp, uid, uname):
                yield m
            return

        # 加入 / 退出 / 跳过
        if msg_raw in ("加入", "退出", "跳过"):
            action = {"加入": "join", "退出": "quit", "跳过": "skip"}[msg_raw]
            resp = engine.step(action, uid, uname, "")
            if resp.get("status") == "ignore":
                return
            async for m in self._emit(event, engine, session_id, resp, uid, uname):
                yield m
            return

        # 其他消息：若本群有对局且当前回合玩家发非 cc 消息 → 提醒前缀
        if is_player and players and players[engine.state["current_turn"]]["id"] == uid:
            yield event.plain_result("💡 落子请使用「cc 诗句」格式（例：cc 床前明月光）。")
        return

    async def _emit(self, event, engine, session_id, resp, uid, uname):
        """统一发送引擎响应 + 落子后续（诗句记录/抽道具/成就/自动结束）。"""
        status = resp.get("status")
        if status == "ignore":
            return
        if resp.get("msg"):
            yield event.plain_result(resp["msg"])
        if resp.get("image"):
            yield event.image_result(resp["image"])
        if status == "error":
            return
        # 落子成功：记录诗句 + 抽道具 + 成就
        if status == "success" and resp.get("msg", "").startswith("[") or "落子成功" in resp.get("msg", ""):
            pass
        # 自动结束判定（落子成功后）
        if status == "success" and resp.get("board_dead"):
            self.active_games.pop(session_id, None)
            yield event.plain_result("🏔 棋盘上已无可落位的空间，游戏自动结束！\n" + engine.generate_text_report())

    async def _active_timeout_monitor(self, session_id, msg_origin):
        try:
            while session_id in self.active_games:
                await asyncio.sleep(2)
                if session_id not in self.active_games:
                    break
                engine = self.active_games[session_id]
                is_timeout, action, msg = engine.check_active_timeout()
                if is_timeout:
                    chain = [msg]
                    if action == "end":
                        self.active_games.pop(session_id, None)
                        try:
                            from astrbot.api.all import Plain as _Plain, MessageChain as _MC
                            await self.context.send_message(msg_origin, _MC([_Plain(msg)]))
                        except Exception as e:
                            logger.error(f"[poetry_flower] 超时通知失败: {e}")
                        break
                    elif action == "skip":
                        try:
                            from astrbot.api.all import Plain as _Plain, Image as _Image, MessageChain as _MC
                            parts = [_Plain(msg)]
                            if hasattr(engine, "render_image"):
                                parts.append(_Image.fromFileSystem(engine.render_image()))
                            await self.context.send_message(msg_origin, _MC(parts))
                        except Exception as e:
                            logger.error(f"[poetry_flower] 超时跳过通知失败: {e}")
        except Exception as e:
            logger.error(f"[poetry_flower] 超时监控任务崩溃: {e}")

    async def terminate(self):
        for task in tuple(self.timeout_tasks.values()):
            task.cancel()
        self.timeout_tasks.clear()
