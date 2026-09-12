# -*- coding: utf-8 -*-
"""纵横飞花令引擎（从原 crossword_poetry.py 改造）。

新增规则：
- 字数池：开局随机「{5,7} 中 1 种」+「{4,6,8,9,10} 中 2 种」共 3 个允许字数，本局落子只能用这些字数
- 系统开局句固定 5 或 7 字（由上层从底座抽取后传入）
- 无任何可落位时自动结束（has_any_placement 判定）
- 道具【文字狱】：清除场上某字及其 8 邻域（清成空白格，可重落+重算分）
- 道具【红杏出墙】：使用者下一句无视边界（界外部分不显示，一次性）
"""

import os
import random
import re

from PIL import Image, ImageDraw, ImageFont

from .base_engine import BaseGameEngine, BOT_ID, BOT_NAME

CELL_SIZE = 40


class CrosswordEngine(BaseGameEngine):
    def __init__(self, session_id, save_dir, width=21, height=21, timeout_seconds=90,
                 save_filename=None, start_verse=None, allowed_lens=None):
        super().__init__(session_id, save_dir, timeout_seconds, save_filename, game_type_tag="crossword")
        self.WIDTH = width
        self.HEIGHT = height
        self.CELL_SIZE = CELL_SIZE
        self.BOARD_W_PX = width * self.CELL_SIZE
        self.BOARD_H_PX = height * self.CELL_SIZE

        # 棋盘 UI 颜色
        self.COLOR_BG = '#F8F9FA'
        self.COLOR_GRID_LINE = '#E0E0E0'
        self.COLOR_TEXT = '#333333'
        self.COLOR_HIGHLIGHT_FILL = '#FFD700'
        self.COLOR_HIGHLIGHT_TEXT = '#E53935'
        self.COLOR_SYSTEM_CELL = '#E6F3FF'
        self.COLOR_PALETTE = [
            '#FFB3BA', '#FFDFBA', '#BAFFC9', '#BAE1FF',
            '#E8BAFF', '#C2F0C2', '#957DAD',
        ]

        current_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(current_dir)
        self.font = None
        self.label_font = None
        for fp in (os.path.join(parent_dir, "STZHONGS.TTF"), os.path.join(current_dir, "STZHONGS.TTF")):
            if os.path.exists(fp):
                try:
                    self.font = ImageFont.truetype(fp, 28)
                    self.label_font = ImageFont.truetype(fp, 16)
                    break
                except Exception:
                    pass
        if self.font is None:
            self.font = ImageFont.load_default()
            self.label_font = ImageFont.load_default()

        if not self.state.get("custom_data"):
            # 字数池：{5,7} 中 1 种（主字数）+ {4,6,8,9,10} 中 2 种
            if allowed_lens is None:
                main_len = random.choice([5, 7])
                extra = random.sample([4, 6, 8, 9, 10], 2)
                allowed_lens = sorted(set([main_len] + extra))
            self.state["custom_data"] = {
                "grid": [[None for _ in range(width)] for _ in range(height)],
                "is_empty": True,
                "pending_verse": None,
                "pending_options": [],
                "pending_player_id": None,
                "player_colors": {"system": self.COLOR_SYSTEM_CELL},
                "allowed_lens": sorted(int(x) for x in allowed_lens),
                "oob_next": {},  # 红杏出墙：{uid: True} 下一句无视边界
            }
            if start_verse:
                start_x = (self.WIDTH - len(start_verse)) // 2
                start_y = self.HEIGHT // 2
                self._execute_placement(start_verse, start_x, start_y, 'H', "system", "系统")
                self.state["history"].append(f"{start_verse} (系统开局)")
                self.save_state()

        self.render_path = os.path.join(save_dir, f"crossword_cache_{session_id}.png")

    # ================= 字数池 =================

    def allowed_lens(self):
        return list(self.state["custom_data"].get("allowed_lens", [5, 7]))

    def min_allowed_len(self):
        return min(self.allowed_lens()) if self.allowed_lens() else 4

    # ================= 领地 =================

    def _get_player_color(self, player_id):
        colors = self.state["custom_data"]["player_colors"]
        if player_id not in colors:
            used_colors = set(colors.values())
            available_colors = [c for c in self.COLOR_PALETTE if c not in used_colors]
            if available_colors:
                colors[player_id] = random.choice(available_colors)
            else:
                colors[player_id] = "#{:06x}".format(random.randint(0, 0xFFFFFF))
        return colors[player_id]

    def _calculate_territory_scores(self):
        all_players = self.state["players"] + self.state.get("quit_players", [])
        scores = {p['name']: 0 for p in all_players}
        grid = self.state["custom_data"]["grid"]
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                cell = grid[y][x]
                if cell and cell['owner'] in scores:
                    scores[cell['owner']] += 1
        for p in all_players:
            if p['name'] in scores:
                p['score'] = scores[p['name']]

    # ================= 落子判定 =================

    def check_collision(self, verse, start_x, start_y, direction, allow_oob=False):
        """检查放置合法性：越界（除非 allow_oob）、同位异字禁止、至少占用一个新格。"""
        grid = self.state["custom_data"]["grid"]
        new_cells_count = 0
        for i, char in enumerate(verse):
            x = start_x + (i if direction == 'H' else 0)
            y = start_y + (0 if direction == 'H' else i)
            if x < 0 or x >= self.WIDTH or y < 0 or y >= self.HEIGHT:
                if allow_oob:
                    continue  # 红杏出墙：界外部分忽略（不显示）
                return False
            if grid[y][x] is not None:
                if grid[y][x]['char'] != char:
                    return False
            else:
                new_cells_count += 1
        return new_cells_count > 0

    def _execute_placement(self, verse, start_x, start_y, direction, player_id, player_name, allow_oob=False):
        color = self._get_player_color(player_id)
        grid = self.state["custom_data"]["grid"]
        intersection_points = []
        for i, char in enumerate(verse):
            x = start_x + (i if direction == 'H' else 0)
            y = start_y + (0 if direction == 'H' else i)
            if x < 0 or x >= self.WIDTH or y < 0 or y >= self.HEIGHT:
                continue  # 红杏出墙：界外不显示
            if grid[y][x] is not None:
                intersection_points.append((x, y))
            old_owner = grid[y][x]['owner'] if grid[y][x] else None
            changes = grid[y][x].get('changes', 0) if grid[y][x] else 0
            if old_owner != player_name:
                changes += 1
            grid[y][x] = {'char': char, 'color': color, 'owner': player_name, 'changes': changes}

        self.state["custom_data"]["is_empty"] = False

        for ix, iy in intersection_points:
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
                nx, ny = ix + dx, iy + dy
                if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                    if grid[ny][nx] is not None:
                        old_owner = grid[ny][nx]['owner']
                        if old_owner != player_name:
                            grid[ny][nx]['changes'] = grid[ny][nx].get('changes', 0) + 1
                        grid[ny][nx]['color'] = color
                        grid[ny][nx]['owner'] = player_name

    # ================= 自动结束判定 =================

    def has_any_placement(self):
        """棋盘上是否还存在任何合法落位（按本局允许字数）。

        判定：对每个已有字格 c，在其横/竖方向上尝试以 c 为诗句中任意一字的位置，
        检查是否存在「连续 L 个格子均为空白或与诗句位置无关」的窗口。
        简化为几何判定：存在一个长度为 L 的窗口（横或竖），窗口内所有格均为空白，
        或窗口内所有已占用格的字与某个假设诗句一致——精确诗句匹配由上层诗句库保证，
        这里只需保证「存在几何可放窗口」：窗口内不含异字冲突即可。
        """
        grid = self.state["custom_data"]["grid"]
        # 收集已有字
        chars = []
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                if grid[y][x] is not None:
                    chars.append((x, y, grid[y][x]['char']))
        if not chars:
            return True  # 空盘（理论上不会出现）
        for L in self.allowed_lens():
            if L > max(self.WIDTH, self.HEIGHT):
                continue
            # 对每个已有字，尝试把它作为诗句第 k 字（k ∈ [0, L)）
            for (cx, cy, _ch) in chars:
                for k in range(L):
                    # 水平窗口：起点 (cx-k, cy)
                    if self._window_ok(cx - k, cy, L, 'H'):
                        return True
                    # 垂直窗口：起点 (cx, cy-k)
                    if self._window_ok(cx, cy - k, L, 'V'):
                        return True
        return False

    def _window_ok(self, sx, sy, L, direction):
        """检查以 (sx,sy) 为起点、长 L 的窗口是否「几何可放」：
        - 窗口必须包含至少一个已有字（保证交叉）
        - 窗口内所有已占用格之间不能冲突（同格同字允许，异字禁止）——
          由于我们不知道未来诗句内容，保守判定为：窗口内已占用格的字必须全部相同
          （即这些格子可以被同一诗句的同字位置覆盖）
        - 至少一个空白格（向外延伸）
        """
        grid = self.state["custom_data"]["grid"]
        occupied_chars = set()
        has_empty = False
        has_occupied = False
        for i in range(L):
            x = sx + (i if direction == 'H' else 0)
            y = sy + (0 if direction == 'H' else i)
            if x < 0 or x >= self.WIDTH or y < 0 or y >= self.HEIGHT:
                return False
            cell = grid[y][x]
            if cell is None:
                has_empty = True
            else:
                has_occupied = True
                occupied_chars.add(cell['char'])
        if not has_empty or not has_occupied:
            return False
        # 窗口内已占用字必须唯一（同一诗句同一字可覆盖多格）
        if len(occupied_chars) != 1:
            return False
        return True

    # ================= 渲染 =================

    def render_image(self, pending_options=None, pending_verse=None):
        image = Image.new('RGB', (self.BOARD_W_PX, self.BOARD_H_PX), color=self.COLOR_BG)
        draw = ImageDraw.Draw(image)
        grid = self.state["custom_data"]["grid"]

        path_highlights = set()
        labels_map = {}
        if pending_options and pending_verse:
            L = len(pending_verse)
            for i, opt in enumerate(pending_options):
                sx, sy = opt['start_x'], opt['start_y']
                direction = opt['dir']
                label_str = str(i + 1)
                for k in range(L):
                    cx = sx + (k if direction == 'H' else 0)
                    cy = sy + (0 if direction == 'H' else k)
                    if cx < 0 or cx >= self.WIDTH or cy < 0 or cy >= self.HEIGHT:
                        continue  # 红杏出墙预览：界外不标
                    path_highlights.add((cx, cy))
                    if k == 0 or k == L - 1:
                        if (cx, cy) not in labels_map:
                            labels_map[(cx, cy)] = []
                        if label_str not in labels_map[(cx, cy)]:
                            labels_map[(cx, cy)].append(label_str)

        for i in range(self.WIDTH + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(line_pos, 0), (line_pos, self.BOARD_H_PX)], fill=self.COLOR_GRID_LINE, width=1)
        for i in range(self.HEIGHT + 1):
            line_pos = i * self.CELL_SIZE
            draw.line([(0, line_pos), (self.BOARD_W_PX, line_pos)], fill=self.COLOR_GRID_LINE, width=1)

        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                cell = grid[y][x]
                x0, y0 = x * self.CELL_SIZE, y * self.CELL_SIZE
                rect_coords = [x0 + 1, y0 + 1, x0 + self.CELL_SIZE - 1, y0 + self.CELL_SIZE - 1]
                is_path = (x, y) in path_highlights
                cell_labels = labels_map.get((x, y))
                if is_path:
                    draw.rectangle(rect_coords, fill=self.COLOR_HIGHLIGHT_FILL)
                elif cell is not None:
                    draw.rectangle(rect_coords, fill=cell['color'])
                if cell_labels:
                    label = "/".join(cell_labels)
                    font_to_use = self.label_font if len(label) > 1 else self.font
                    bbox = draw.textbbox((0, 0), label, font=font_to_use)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    draw.text((x0 + (self.CELL_SIZE - tw) / 2, y0 + (self.CELL_SIZE - th) / 2 - 4), label,
                              fill=self.COLOR_HIGHLIGHT_TEXT, font=font_to_use)
                elif cell is not None:
                    bbox = draw.textbbox((0, 0), cell['char'], font=self.font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    draw.text((x0 + (self.CELL_SIZE - tw) / 2, y0 + (self.CELL_SIZE - th) / 2 - 4), cell['char'],
                              fill=self.COLOR_TEXT, font=self.font)

        image.save(self.render_path)
        return self.render_path

    # ================= 回合处理 =================

    def process_quit(self, user_id, user_name):
        custom = self.state.get("custom_data", {})
        if custom.get("pending_options") and custom.get("pending_player_id") == str(user_id):
            custom["pending_options"] = []
            custom["pending_verse"] = None
        return super().process_quit(user_id, user_name)

    def _finalize_success_turn(self, user_name, verse, title="", author=""):
        self._calculate_territory_scores()
        if title:
            self.state["history"].append(f"{verse} ({author}·《{title}》)")
        else:
            self.state["history"].append(verse)
        self.state["turn_count"] += 1
        self.record_round_scores()
        self.next_turn()
        self.save_state()

        players = self.state["players"]
        curr_p = next((p for p in players if p['name'] == user_name), None)
        next_name = players[self.state["current_turn"]]["name"] if players else ""
        msg = (
            f"[{user_name}] 落子成功！\n"
            f"诗句：{verse} " + (f"({author})" if author else "") + "\n"
            f"当前领地总分：{curr_p['score'] if curr_p else 0} 格\n"
            f"{'-' * 15}\n"
            f"👉 下一位：[{next_name}]"
        )
        return {"status": "success", "msg": msg, "image": self.render_image()}

    def _use_wenziyu(self, user_id, user_name, payload):
        """文字狱：清除场上所有指定汉字及其 8 邻域（清成空白格，可重落+重算分）。不受回合限制。"""
        ch = (payload or "").strip()[:1]
        if not ch or not ('\u4e00' <= ch <= '\u9fff'):
            return {"status": "error", "msg": "用法：/诗词道具 文字狱 汉字"}
        grid = self.state["custom_data"]["grid"]
        cleared = 0
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                if grid[y][x] is not None and grid[y][x]['char'] == ch:
                    for dx, dy in [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT and grid[ny][nx] is not None:
                            grid[ny][nx] = None
                            cleared += 1
        self._calculate_territory_scores()
        self.save_state()
        return {"status": "success",
                "msg": f"⛓ 【文字狱】发动！场上所有「{ch}」及其周围一圈共清除 {cleared} 格。",
                "image": self.render_image()}

    def step(self, action_type, user_id, user_name, payload="", verse_meta=None):
        """处理动作。

        action_type: join / quit / skip / play / choice / item_wz(文字狱) / item_hx(红杏出墙标记)
        verse_meta:  落子成功时由上层回填 (title, author)——引擎不查库，出处由上层校验时取得
        """
        self.update_activity()
        user_id = str(user_id)

        if action_type == "join":
            return self.process_join(user_id, user_name)
        if action_type == "quit":
            return self.process_quit(user_id, user_name)

        if not self.state["players"]:
            return {"status": "ignore"}

        custom = self.state["custom_data"]

        # ===== 道具动作（不受回合限制，任何玩家可用）=====
        if action_type == "item_wz":
            return self._use_wenziyu(user_id, user_name, payload)
        if action_type == "item_hx":
            custom.setdefault("oob_next", {})[user_id] = True
            self.save_state()
            return {"status": "success",
                    "msg": "🌸 【红杏出墙】发动！你的下一句诗句可以无视棋盘边界（超出边界的部分不会显示）。"}

        current_p = self.state["players"][self.state["current_turn"]]

        # ===== 抉择分支 =====
        if custom.get("pending_options"):
            if user_id != custom["pending_player_id"]:
                return {"status": "ignore"}
            if payload.strip().lower() in ['取消', 'q']:
                custom["pending_options"] = []
                custom["pending_verse"] = None
                custom["pending_player_id"] = None
                self.save_state()
                return {"status": "success", "msg": "已取消操作。"}
            if payload.strip().isdigit():
                choice_idx = int(payload.strip()) - 1
                options = custom["pending_options"]
                if 0 <= choice_idx < len(options):
                    opt = options[choice_idx]
                    allow_oob = bool(custom.get("oob_next", {}).get(user_id))
                    self._execute_placement(custom["pending_verse"], opt['start_x'], opt['start_y'],
                                            opt['dir'], user_id, user_name, allow_oob=allow_oob)
                    custom.get("oob_next", {}).pop(user_id, None)
                    verse_cache = custom["pending_verse"]
                    custom["pending_options"] = []
                    custom["pending_verse"] = None
                    custom["pending_player_id"] = None
                    return self._finalize_success_turn(user_name, verse_cache)
            image_path = self.render_image(pending_options=custom["pending_options"], pending_verse=custom["pending_verse"])
            msg = "选择无效。\n请直接发送图片中首尾两端标记的【数字】（如：1）。"
            return {"status": "success", "msg": msg, "image": image_path}

        # ===== 跳过 =====
        if action_type == "skip":
            if len(self.state["players"]) <= 1:
                return {"status": "error", "msg": "当前只有你一个人在玩，没法跳过哦！"}
            timeout_limit = self.get_timeout()
            if time.time() - self.last_active_time < timeout_limit:
                rem = int(timeout_limit - (time.time() - self.last_active_time))
                return {"status": "error", "msg": f"还没到超时时间，请再给 TA {rem} 秒吧！"}
            curr_p = self.state["players"][self.state["current_turn"]]
            if custom.get("pending_options") and custom["pending_player_id"] == curr_p["id"]:
                custom["pending_options"] = []
                custom["pending_verse"] = None
            self.next_turn()
            self.update_activity()
            self.save_state()
            next_p = self.state["players"][self.state["current_turn"]]
            return {"status": "success", "msg": f"已强制跳过超时的 [{curr_p['name']}]！\n👉 下一位：[{next_p['name']}]"}

        if user_id != current_p['id']:
            return {"status": "ignore"}

        # ===== 常规落子（play）=====
        verse = re.sub(r'[^\u4e00-\u9fa5]', '', payload or "")
        if not verse:
            return {"status": "ignore"}

        # 字数池校验
        allowed = self.allowed_lens()
        if len(verse) not in allowed:
            return {"status": "error",
                    "msg": f"落子失败！本局仅允许 {'、'.join(str(x) for x in allowed)} 字诗句，当前 {len(verse)} 字。"}

        used_verses = [re.sub(r'[^\u4e00-\u9fa5]', '', h.split(' (')[0]) for h in self.state.get("history", [])]
        if verse in used_verses:
            return {"status": "error", "msg": f"诗句重复！【{verse}】已在棋盘上，请换一句。"}

        # 出处由上层校验并传入（title, author）
        title, author = (verse_meta or (None, None))

        allow_oob = bool(custom.get("oob_next", {}).get(user_id))
        valid_placements = []
        seen_placements = set()
        grid = custom["grid"]
        for y in range(self.HEIGHT):
            for x in range(self.WIDTH):
                cell = grid[y][x]
                if cell is not None and cell['char'] in verse:
                    for idx, c in enumerate(verse):
                        if c == cell['char']:
                            start_x_h = x - idx
                            signature_h = (start_x_h, y, 'H')
                            if signature_h not in seen_placements and self.check_collision(verse, start_x_h, y, 'H', allow_oob=allow_oob):
                                seen_placements.add(signature_h)
                                valid_placements.append({'start_x': start_x_h, 'start_y': y, 'dir': 'H'})
                            start_y_v = y - idx
                            signature_v = (x, start_y_v, 'V')
                            if signature_v not in seen_placements and self.check_collision(verse, x, start_y_v, 'V', allow_oob=allow_oob):
                                seen_placements.add(signature_v)
                                valid_placements.append({'start_x': x, 'start_y': start_y_v, 'dir': 'V'})

        if not valid_placements:
            return {"status": "error",
                    "msg": "落子失败！找不到合法的交叉点，或者该诗句完全与已有汉字重叠（必须向外延伸，占用至少一个新格子）。"}

        if len(valid_placements) == 1:
            opt = valid_placements[0]
            self._execute_placement(verse, opt['start_x'], opt['start_y'], opt['dir'], user_id, user_name, allow_oob=allow_oob)
            custom.get("oob_next", {}).pop(user_id, None)
            resp = self._finalize_success_turn(user_name, verse, title, author)
            resp["board_dead"] = not self.has_any_placement()
            return resp
        else:
            custom["pending_verse"] = verse
            custom["pending_player_id"] = user_id
            custom["pending_options"] = valid_placements
            self.save_state()
            image_path = self.render_image(pending_options=valid_placements, pending_verse=verse)
            prompt = (
                f"发现 {len(valid_placements)} 种合法的落子方式！\n"
                f"图片中已用金色高亮了所有可选路线。\n"
                f"请查看你要选的那条路线，发送首尾两端对应的【数字】（如：1）即可落子。\n"
                f"（发送 取消 可放弃本次落子）"
            )
            return {"status": "pending", "msg": prompt, "image": image_path}
