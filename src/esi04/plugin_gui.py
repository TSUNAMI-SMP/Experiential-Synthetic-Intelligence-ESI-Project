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
        layout.addWidget(self.details, 1)
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
            self.actions.clear()
            return
        actor = observation.actor
        self.body.setText(f"満腹度 {actor.food}/20 / 体力 {actor.health:g}/{actor.max_health:g} / "
                          f"{actor.dimension} / 座標 {actor.x:.1f}, {actor.y:.1f}, {actor.z:.1f}"
                          + ("（前回取得した状態・現在は未接続）" if not status.get("plugin_connected") else ""))
        if self.observation_id == observation.id:
            return
        self.observation_id = observation.id
        selected = self.actions.currentData()
        self.actions.clear()
        manifest = session.active_manifest()
        for identifier, action in action_menu(manifest, observation).items():
            if action:
                label = {"look": "視線を変える", "move": "少し歩く", "go_to": "近くへ歩く", "mine": "採掘する",
                         "eat": "食事する", "select_slot": "持つ物を変える", "use": "使う", "place": "ブロックを置く", "attack": "攻撃する"}.get(action.kind, action.kind)
                target = next((t for t in observation.targets if t.id == action.target_id), None)
                if target:
                    label += f"：{target.name}（{target.distance:.1f}m）"
                elif action.kind in {"eat", "select_slot"}:
                    item = next((i for i in observation.inventory if i.slot == action.slot), None)
                    label += "："+(item.name if item else str(action.slot+1))
                else:
                    label += "："+{"look_left": "左", "look_right": "右", "look_down": "下", "look_ahead": "正面",
                                     "forward": "前", "back": "後ろ", "left": "左", "right": "右",
                                     "jump": "ジャンプ", "jump_forward": "前へジャンプ"}.get(identifier, "")
                self.actions.addItem(label, identifier)
        index = self.actions.findData(selected)
        if index >= 0:
            self.actions.setCurrentIndex(index)
        lines = ["所持品："]+[f"{item.slot+1}: {item.name} ×{item.count}" for item in observation.inventory]
        lines += ["", "見えている周囲："]+[f"{t.name} / 距離 {t.distance:.1f}" for t in observation.targets[:20]]
        lines += ["", "現在の行動・観測結果：", str(observation.action.get("detail", "まだ操作していません。")),
                  "", f"記録した試行 {state.attempts} / 実際の変化を確認 {state.confirmed_changes}"]
        self.details.setPlainText("\n".join(lines))
