"""Install declarative plugins, connect a world and inspect ESI's actual body/actions."""


from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi02.desktop_preferences import write_json
from esi04.minecraft import action_menu
from esi04.plugins import Connection


class PluginPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.shown_session = None
        self.observation_id = None
        self.displayed_autonomous = None
        layout = QVBoxLayout(self)
        intro = QLabel("ESIの人格・記憶を保ったまま、生活する環境を切り替えます。Minecraftでは仮想の部屋・水分・温度・音の不快を停止し、ゲームの体力と満腹度を使います。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.plugins = QComboBox()
        row.addWidget(self.plugins, 1)
        self.button(row, "プラグイン定義を追加", self.install)
        self.button(row, "仮想の部屋へ戻る", lambda: self.send("virtual"))
        layout.addLayout(row)
        form = QFormLayout()
        self.endpoint = QLineEdit("http://127.0.0.1:8766")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Minecraftで /esi link を実行し、表示されたコードを貼り付ける")
        form.addRow("接続先", self.endpoint)
        form.addRow("接続コード", self.token)
        layout.addLayout(form)
        self.remember = QCheckBox("このPCに接続先・コードを保存する（診断には含めません）")
        layout.addWidget(self.remember)
        row = QHBoxLayout()
        self.button(row, "ワールドに接続してESIを参加させる", self.connect)
        self.button(row, "切断", lambda: self.send("disconnect"))
        self.button(row, "行動を停止", lambda: self.send("stop"))
        layout.addLayout(row)
        self.status = QLabel("Minecraftのワールドに入り、/esi link を実行してください。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.goal = QLineEdit()
        self.goal.setPlaceholderText("目標・気になること（攻略手順を決めず、自分で周囲を調べることもできます）")
        layout.addWidget(self.goal)
        row = QHBoxLayout()
        self.autonomous = QCheckBox("Minecraftで自分から行動を選ぶ")
        row.addWidget(self.autonomous)
        self.button(row, "目標と自律活動の設定を適用", self.options)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.actions = QComboBox()
        row.addWidget(self.actions, 1)
        self.button(row, "選んだ操作を1回試す", self.manual)
        layout.addLayout(row)
        self.body = QLabel()
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.learning_view, self.places_view = QTextEdit(), QTextEdit()
        self.learning_view.setReadOnly(True)
        self.places_view.setReadOnly(True)
        for view in (self.details, self.learning_view, self.places_view):
            view.setMinimumHeight(240)
        tabs = QTabWidget()
        tabs.addTab(self.details, "現在の観察・操作")
        tabs.addTab(self.learning_view, "小さな目標・経験")
        tabs.addTab(self.places_view, "見た場所の記憶")
        layout.addWidget(tabs, 1)
        help_text = QLabel("Fabric 1.21.11にFabric API・Carpet・ESI Bridge MODを導入します。\n"
                           "画面認識モデルは使いません。操作・周囲・食事の結果はMODから取得します。\n"
                           "接続が切れた場合やワールドから退出した場合は活動を止めます。再起動時に自律活動を自動再開しません。")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.reload()

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)

    @property
    def credentials_path(self):
        path = self.window.session.store.path
        return path.with_name(path.stem+"-plugin-connection.json")

    def reload(self):
        self.shown_session = self.window.session
        self.plugins.clear()
        for manifest in self.window.session.plugin_catalog.manifests():
            self.plugins.addItem(manifest.name, manifest.id)
        self.goal.setText(self.window.session.plugins.goal)
        self.endpoint.setText("http://127.0.0.1:8766")
        self.token.clear()
        self.remember.setChecked(False)
        try:
            if self.credentials_path.exists():
                saved = Connection.model_validate_json(self.credentials_path.read_text(encoding="utf-8"))
                self.endpoint.setText(saved.endpoint)
                self.token.setText(saved.token)
                self.remember.setChecked(True)
        except (OSError, ValueError):
            self.status.setText("保存した接続設定を読み取れませんでした。入力し直してください。")
        self.observation_id = None
        self.displayed_autonomous = None

    def send(self, operation, values=None):
        if not self.window.runtime:
            self.status.setText("本体上部でESIを開始してから接続・操作してください。")
            return False
        return self.window.send("plugin", (operation, values or {}))

    def connect(self):
        try:
            connection = Connection(endpoint=self.endpoint.text().strip(), token=self.token.text().strip())
            if self.remember.isChecked():
                write_json(self.credentials_path, connection.model_dump())
            else:
                self.credentials_path.unlink(missing_ok=True)
            if self.send("connect", {"id": self.plugins.currentData(), "connection": connection.model_dump()}):
                self.status.setText("ワールドへ接続しています。Minecraftを一時停止している場合は再開してください。")
        except Exception as exception:
            self.status.setText(str(exception))

    def options(self):
        self.send("options", {"autonomous": self.autonomous.isChecked(), "goal": self.goal.text()})

    def manual(self):
        if self.actions.currentData():
            self.send("manual", {"id": self.actions.currentData()})

    def install(self):
        path, _ = QFileDialog.getOpenFileName(self, "プラグイン定義を追加", "", "プラグイン定義 (*.json)")
        if path:
            try:
                manifest = self.window.session.plugin_catalog.install(path)
                self.reload()
                self.status.setText(manifest.name+"を追加しました。接続するまで実行しません。")
            except Exception as exception:
                self.status.setText(str(exception))

    def refresh(self):
        if self.shown_session is not self.window.session:
            self.reload()
        session = self.window.session
        state, status = session.plugins, session.status
        if self.displayed_autonomous != state.autonomous:
            self.displayed_autonomous = state.autonomous
            self.autonomous.setChecked(state.autonomous)
        if status.get("plugin_error"):
            self.status.setText(status["plugin_error"])
        elif state.active_id:
            phase = {"connecting": "接続処理中", "deciding": "次の行動を考えています", "connected": "接続済み", "disconnected": "切断"}.get(status.get("plugin_phase"), "切断")
            self.status.setText("生活環境：Minecraft / "+phase+" / 自律活動："+("ON" if state.autonomous else "OFF"))
        observation = state.observation
        if not observation or not state.active_id:
            self.body.setText("Minecraftの現在の身体状態は、接続後に表示します。")
            self.details.clear()
            self.learning_view.clear()
            self.places_view.clear()
            self.actions.clear()
            return
        actor = observation.actor
        self.body.setText(f"満腹度 {actor.food}/20 / 体力 {actor.health:g}/{actor.max_health:g} / "
                          f"{actor.dimension} / 座標 {actor.x:.1f}, {actor.y:.1f}, {actor.z:.1f}"
                          + ("（前回取得した状態・現在は未接続）" if not status.get("plugin_connected") else ""))
        self.show_learning(state, observation)
        if self.observation_id == observation.id:
            return
        self.observation_id = observation.id
        selected = self.actions.currentData()
        self.actions.clear()
        manifest = session.active_manifest()
        for identifier, action in action_menu(manifest, observation).items():
            if action:
                label = {"look": "視線を変える", "move": "少し歩く", "go_to": "近くへ歩く", "mine": "採掘する",
                         "eat": "食事する", "select_slot": "持つ物を変える", "use": "使う・開く", "place": "ブロックを置く", "attack": "攻撃する",
                         "craft": "クラフトする", "deposit": "収納へ預ける", "withdraw": "収納から取り出す", "close_container": "収納・作業画面を閉じる"}.get(action.kind, action.kind)
                target = next((t for t in observation.targets if t.id == action.target_id), None)
                if target:
                    label += f"：{target.name}（{target.distance:.1f}m）"
                elif action.kind in {"eat", "select_slot", "deposit"}:
                    item = next((i for i in observation.inventory if i.slot == action.slot), None)
                    label += "："+(item.name if item else "何も持たない" if identifier == "empty_hand" else str(action.slot+1))
                elif action.kind == "craft":
                    recipe = next(r for r in observation.recipes if r.id == action.recipe_id)
                    label += f"：{recipe.output} ×{recipe.count}"
                elif action.kind == "withdraw":
                    item = next(i for i in observation.container.items if i.slot == action.container_slot)
                    label += f"：{item.name} ×{item.count}"
                else:
                    label += "："+{"look_left": "左", "look_right": "右", "look_down": "下", "look_ahead": "正面",
                                     "forward": "前", "back": "後ろ", "left": "左", "right": "右",
                                     "jump": "ジャンプ", "jump_forward": "前へジャンプ"}.get(identifier, "")
                self.actions.addItem(label, identifier)
        index = self.actions.findData(selected)
        if index >= 0:
            self.actions.setCurrentIndex(index)
        held = next((i.name for i in observation.inventory if i.selected), "なし")
        lines = ["手に持っている物："+held, "", "所持品（手に持っている物とは区別）："]+[
            f"{item.slot+1}: {item.name} ×{item.count}"+(" [手持ち]" if item.selected else "") for item in observation.inventory]
        lines += ["", "見えている周囲："]+[f"{t.name} / 距離 {t.distance:.1f}" for t in observation.targets[:20]]
        lines += ["", "現在の行動・観測結果：", str(observation.action.get("detail", "まだ操作していません。")),
                  "", f"記録した試行 {state.attempts} / 実際の変化を確認 {state.confirmed_changes}"]
        lines += ["", "ゲーム内で解放されたレシピ："]+[
            f"{r.output} ×{r.count} / {'今作れる' if r.craftable else '材料・作業場所を確認'} / {'手持ちの2×2' if r.station == 'inventory' else '作業台の3×3'}\n  材料: {', '.join(r.ingredients)}"
            for r in observation.recipes[:24]]
        if observation.container:
            lines += ["", "実際に開いている収納：", observation.container.kind]+[
                f"{i.slot+1}: {i.name} ×{i.count}" for i in observation.container.items]
        self.show_text(self.details, "\n".join(lines))

    @staticmethod
    def show_text(view, text):
        if view.toPlainText() != text:
            scroll = view.verticalScrollBar().value()
            view.setPlainText(text)
            view.verticalScrollBar().setValue(scroll)

    def show_learning(self, state, observation):
        learning, plan, actor = state.learning, state.learning.plan, observation.actor
        stages = {"proposed": "予定", "running": "試行中", "confirmed": "観察で達成を確認", "unobserved": "未確認・見直し",
                  "failed": "失敗・見直し", "paused": "保留"}
        checks = {"observe": "周囲を観察する", "held": "指定した物を手に持つ", "inventory_gain": "所持品が増える",
                  "inventory_decrease": "所持品が減る", "food_increase": "満腹度が回復する", "block_change": "対象のブロックが変わる",
                  "approach": "対象へ近づく", "container_open": "収納・作業画面が開く", "none": "今回は確認しない"}
        title = "前のワールドで考えた小さな目標：" if plan.world_id and plan.world_id != observation.world_id else "今回の小さな目標："
        lines = ["現在の目標："+state.goal, title+plan.step.purpose, "状態："+stages[plan.stage],
                 "重視した欲求："+{"hunger": "食事", "safety": "安全", "curiosity": "好奇心", "growth": "成長",
                                 "social": "交流", "achievement": "達成", "autonomy": "自立"}[plan.focus],
                 "確認する変化："+checks[plan.step.check]+(" / "+plan.step.item if plan.step.item else ""),
                 "選んだ理由（生成した説明）："+plan.reason, "観察からの見直し："+plan.review,
                 "", "続きに考えたいこと（未実行の計画）："]+plan.step.following
        lines += ["", "条件ごとの試行（他の道具・場所でも成立するとは限りません）："]+[
            f"{t.action} / {t.dimension} / 対象 {t.target or 'なし'} / 手持ち {t.held}\n"
            f"  試行{t.trials} / 変化確認{t.effects} / 小目標確認{t.goal_checks}\n  {t.last_detail}\n  出典: {t.last_source}"
            for t in reversed(learning.trials) if t.world_id == observation.world_id][:12]
        lines += ["", "最近の観察された経験："]+[
            f"{e.get('kind')} / {'小目標確認' if e.get('goal_confirmed') else '目標は未確認'} / {e.get('detail')}\n出典: {e.get('source_observation')}"
            for e in reversed([e for e in learning.episodes if e.get("world_id") == observation.world_id][-6:])]
        lines += ["", "教わった説明は会話の知識として保存し、実験結果とは分けます。原因の推測やこの説明自体を成功・報酬にはしません。"]
        self.show_text(self.learning_view, "\n".join(lines))
        places = [p for p in learning.places if p.world_id == observation.world_id and p.dimension == actor.dimension]
        places.sort(key=lambda p: sum((getattr(p, a)-getattr(actor, a))**2 for a in "xyz"))
        text = "過去に見た範囲だけの記憶です。現在も同じ状態とは限りません。\n隠れた鉱石や未開封の収納の中身は含めません。\n\n"+"\n\n".join(
            f"{p.x:.0f}, {p.y:.0f}, {p.z:.0f} / {'訪問済み' if p.visited else '視認のみ'} / 最終確認 tick {p.tick}\n"
            +", ".join(p.names)+(f"\n{', '.join(p.risks)}" if p.risks else "")+f"\n出典: {p.observation_id}" for p in places[:40])
        stores = [s for s in learning.storage if (s.world_id, s.dimension) == (observation.world_id, actor.dimension)]
        if stores:
            text += "\n\n過去に開いた収納（今の中身は開いて再確認）：\n"+"\n\n".join(
                f"{s.x:.0f}, {s.y:.0f}, {s.z:.0f} / {s.items or '空だった'} / 最終確認 tick {s.tick}\n出典: {s.source_id}"
                for s in stores[-12:])
        self.show_text(self.places_view, text)
