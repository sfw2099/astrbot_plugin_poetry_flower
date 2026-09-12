# -*- coding: utf-8 -*-
"""飞花令基础引擎：存档/玩家/回合框架（从原 base_game.py 精简）。

- 去除 bot 与数据库直连（库校验/随机出题由上层注入回调）
- 保留：存档读写、加入/退出、回合流转、超时探测、领地快照、战报
"""

import json
import os
import re
import time

BOT_ID = "__bot_poetry__"
BOT_NAME = "🤖诗词AI"


class BaseGameEngine:
    def __init__(self, session_id, save_dir, timeout_seconds=90, save_filename=None, game_type_tag="crossword"):
        self.session_id = str(session_id)
        self.save_dir = save_dir
        self.game_type_tag = game_type_tag

        if save_filename:
            self.save_file = os.path.join(save_dir, save_filename)
        else:
            timestamp = int(time.time())
            self.save_file = os.path.join(save_dir, f"game_{self.session_id}_{self.game_type_tag}_{timestamp}.json")

        self.state = {
            "game_type": self.__class__.__name__,
            "players": [],
            "current_turn": 0,
            "turn_count": 0,
            "history": [],
            "round_records": [],
            "timeout_seconds": timeout_seconds,
            "custom_data": {},
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        self.last_active_time = time.time()
        os.makedirs(self.save_dir, exist_ok=True)

    def get_timeout(self):
        return self.state.get("timeout_seconds", 90)

    def update_activity(self):
        self.last_active_time = time.time()

    def check_active_timeout(self):
        if time.time() - self.last_active_time > self.get_timeout():
            if len(self.state["players"]) == 0:
                return True, "end", "飞花令超时无人加入，已自动解散。"
            elif len(self.state["players"]) == 1:
                return True, "end", "飞花令只有 1 人加入且长时间未操作，已自动解散。"
            else:
                curr_p = self.state["players"][self.state["current_turn"]]
                self.next_turn()
                self.update_activity()
                self.save_state()
                next_p = self.state["players"][self.state["current_turn"]]
                custom = self.state.get("custom_data", {})
                if custom.get("pending_options"):
                    custom["pending_options"] = []
                    custom["pending_verse"] = None
                return True, "skip", f"⏳ [{curr_p['name']}] 思考超时！已自动剥夺其回合。\n👉 现在轮到：[{next_p['name']}]"
        return False, "", ""

    def save_state(self):
        try:
            with open(self.save_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[flower] 存档失败: {e}")

    def load_state(self):
        if os.path.exists(self.save_file):
            try:
                with open(self.save_file, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
                return True
            except Exception:
                return False
        return False

    def process_join(self, user_id, user_name):
        players = self.state["players"]
        if any(p['id'] == str(user_id) for p in players):
            return {"status": "ignore"}
        players.append({"id": str(user_id), "name": user_name, "score": 0})
        self.update_activity()
        self.save_state()
        msg = f"玩家[{user_name}]成功加入游戏！\n当前排位：第 {len(players)} 号位。"
        if len(players) == 1:
            msg += "\n您是首位玩家，可以直接发送 cc 诗句开始接龙！"
        else:
            msg += f"\n👉 当前轮到：[{players[self.state['current_turn']]['name']}]"
        return {"status": "success", "msg": msg}

    def process_quit(self, user_id, user_name):
        players = self.state["players"]
        idx = -1
        for i, p in enumerate(players):
            if p['id'] == str(user_id):
                idx = i
                break
        if idx == -1:
            return {"status": "ignore"}
        curr_turn = self.state["current_turn"]
        if idx < curr_turn:
            self.state["current_turn"] -= 1
        quitter = players.pop(idx)
        self.state.setdefault("quit_players", []).append(quitter)
        self.update_activity()
        self.save_state()
        if not players:
            self.state["current_turn"] = 0
            return {"status": "success", "msg": f"玩家[{user_name}]已退出游戏。\n当前对局已无活跃玩家，等待新玩家加入。"}
        self.state["current_turn"] %= len(players)
        next_p = players[self.state["current_turn"]]
        msg = f"玩家[{user_name}]已退出游戏。"
        if idx == curr_turn:
            msg += f"\n👉 轮次顺延，现在轮到：[{next_p['name']}]"
        return {"status": "success", "msg": msg}

    def next_turn(self):
        players = self.state["players"]
        if len(players) > 1:
            self.state["current_turn"] = (self.state["current_turn"] + 1) % len(players)

    def record_round_scores(self):
        all_players = self.state["players"] + self.state.get("quit_players", [])
        snapshot = {p['name']: p['score'] for p in all_players}
        self.state["round_records"].append({
            "round": self.state["turn_count"],
            "scores": snapshot,
        })

    def generate_text_report(self):
        all_players = self.state["players"] + self.state.get("quit_players", [])
        if not all_players:
            return "暂无玩家参与，无法生成战报。"
        report = [f"【{self.game_display_name()}】对局战报", "=" * 20]
        players_sorted = sorted(all_players, key=lambda x: x.get("score", 0), reverse=True)
        report.append("最终排名：")
        for i, p in enumerate(players_sorted, 1):
            status_tag = " (已退出)" if p in self.state.get("quit_players", []) else ""
            report.append(f"{i}. [{p['name']}]{status_tag} - {p.get('score', 0)} 分")
        report.append("-" * 15)
        report.append(f"游戏总回合：{self.state['turn_count']}")
        report.append(f"共落子诗句：{len(self.state['history'])} 句")
        if self.state["round_records"]:
            report.append("-" * 15)
            report.append("📈 战局逆转回顾：")
            records = self.state["round_records"]
            step = max(1, len(records) // 5)
            for idx in range(0, len(records), step):
                r = records[idx]
                report.append(f"[第{r['round']}回合] " + ", ".join([f"{k}:{v}" for k, v in r['scores'].items()]))
            if (len(records) - 1) % step != 0:
                r = records[-1]
                report.append(f"[最终回合] " + ", ".join([f"{k}:{v}" for k, v in r['scores'].items()]))
        return "\n".join(report)

    def game_display_name(self):
        return "纵横飞花令"

    def step(self, action_type, user_id, user_name, payload=""):
        raise NotImplementedError
