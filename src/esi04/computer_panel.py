"""A compact, staged screen/teaching/play interface with a source-linked visual preview."""

import os
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esi.models import uid, utc
from esi04.computer_models import ScreenTeaching
from esi04.games import Binding, GameProfile, presets, teach_color

HELP = """# 画面を見せて、操作を教える
1. 共有するモニターを選び、画面共有を開始します。
2. **3秒後のウィンドウを対象にする**を押し、3秒以内にゲームへ切り替えます。
3. ESIへ戻り、**画像を更新**で対象の画像を確認します。対象以外のウィンドウを操作しません。
4. **見る**で読み取りを試します。初回の画像AIは起動に時間がかかります。CPUでは1〜2分かかる場合があります。

## ゲームの教え方
- **ゲームを教える**でslither.io / 矢印キー / WASDを選び、プリセットを読み込みます。
- 目的・ルールを入力し、操作表を編集します。`W+SPACE`で同時押し、長さはミリ秒です。マウスは`left / right / middle`です。
- 画像上でゲーム部分だけをドラッグして囲みます。ブラウザーのタブやアドレス欄は含めません。
- **色の目印**は、自分・目標・危険を選んで画像の色の中央をクリックして教えます。異なる色は複数登録できます。
- slither.ioでは自分の頭をクリックし、**自分の頭は画面の同じ位置**をONにします。食べる粒と避けたいヘビの色を複数教えます。
- 移動する2Dキャラクターは同じ位置をOFFにし、自分の色も教えます。色が似た物体の区別は苦手です。
- **画像AI**モードはルールの文章と実演を参照して操作を選びます。判断が2秒より遅ければ、古い画面を操作せず止まります。
- **実演を記録**は3秒後に開始します。短く操作し、キーやボタンを離してからESIへ戻り終了します。記録には前後の画面・長押し・マウス移動が残ります。
- 色の目印モードは登録した色へ向かい、登録した危険を避ける専用の処理です。自由なルールや実演の操作選択には画像AIモードを使います。
- **教えた内容を保存**し、まず**操作を1回試す**、次に**5秒プレイ**を試します。ゲームの中をクリックしてから開始してください。

**F8**または**すべて停止**ですぐ停止し、押していたキーとマウスを離します。別ウィンドウへ切り替えた場合も入力を止めます。

これは教えた操作で試行する機能です。実演だけでゲーム全体を習得する訓練や、スコア・勝敗の自動判定は行いません。
失敗したら目印・ゲーム範囲・操作表を直して見せ直します。最初は低速で色を区別しやすい2Dゲームが向いています。
"""


class ScreenPreview(QWidget):
    marked = Signal(str, object)
    region_selected = Signal(object)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(240, 170)
        self.setMaximumHeight(400)
        self.frame, self.image = None, QImage()
        self.mode, self.origin = "region", None
        self.region, self.cues = [0, 0, 1000, 1000], []
        self.area = QRectF()

    def show_frame(self, frame):
        self.frame = frame
        self.image = QImage(frame.path)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0c1823"))
        if self.image.isNull():
            painter.setPen(QColor("#b8cad8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "対象の画像をここに表示します")
            return
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        self.area = QRectF((self.width()-size.width())/2, (self.height()-size.height())/2, size.width(), size.height())
        painter.drawImage(self.area, self.image)
        painter.setPen(QPen(QColor("#70dccc"), 2))
        x1, y1, x2, y2 = self.region
        painter.drawRect(QRectF(self.area.x()+x1*self.area.width()/1000, self.area.y()+y1*self.area.height()/1000,
                                (x2-x1)*self.area.width()/1000, (y2-y1)*self.area.height()/1000))
        for cue in self.cues:
            x, y = cue.point
            painter.setPen(QPen(QColor(*cue.rgb), 3))
            painter.drawEllipse(self.area.x()+x*self.area.width()/1000-4,
                                self.area.y()+y*self.area.height()/1000-4, 8, 8)

    def normalized(self, point):
        if not self.area.contains(point):
            return None
        return [min(1000, max(0, round((point.x()-self.area.x())*1000/self.area.width()))),
                min(1000, max(0, round((point.y()-self.area.y())*1000/self.area.height())))]

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.frame:
            self.origin = self.normalized(event.position())

    def mouseReleaseEvent(self, event):
        point, origin = self.normalized(event.position()), self.origin
        self.origin = None
        if point is None or origin is None or not self.frame:
            return
        if self.mode == "region":
            region = [min(origin[0], point[0]), min(origin[1], point[1]),
                      max(origin[0], point[0]), max(origin[1], point[1])]
            if region[2]-region[0] >= 50 and region[3]-region[1] >= 50:
                self.region = region
                self.region_selected.emit(region)
        else:
            self.marked.emit(self.mode, point)
        self.update()


class ComputerPanel(QWidget):
    def __init__(self, window, supervisor):
        super().__init__()
        self.window, self.supervisor = window, supervisor
        self.profile = presets("slither")
        self.loaded_skills, self.displayed_generation = None, None
        self.countdown_token, self.started_at = 0, None
        self.last_observation = None
        self.last_error = None
        self.expanded_dialog = None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.screens = QComboBox()
        self.refresh_screens()
        top.addWidget(QLabel("共有する画面"))
        top.addWidget(self.screens, 1)
        self.button(top, "共有を開始・設定を適用", self.apply)
        self.button(top, "すべて停止（F8）", self.stop)
        self.button(top, "操作画面を広く開く", self.open_wide)
        self.button(top, "教え方", self.help)
        layout.addLayout(top)
        permissions = QHBoxLayout()
        self.allow_observe = QCheckBox("見る")
        self.allow_control = QCheckBox("マウス・キー操作")
        self.allow_record = QCheckBox("実演を記録")
        self.allow_move = QCheckBox("アバターの移動")
        self.hide_avatar = QCheckBox("このタブではアバターを隠す")
        self.hide_avatar.setChecked(True)
        for check in (self.allow_observe, self.allow_control, self.allow_record, self.allow_move, self.hide_avatar):
            permissions.addWidget(check)
        layout.addLayout(permissions)
        target = QHBoxLayout()
        self.button(target, "3秒後のウィンドウを対象にする", self.choose_target)
        self.button(target, "モニター全体を見る", self.clear_target)
        self.button(target, "画面一覧を更新", self.refresh_screens)
        layout.addLayout(target)
        self.status = QLabel("画面共有を開始し、対象ウィンドウを選んでください。")
        self.status.setWordWrap(True)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message.setStyleSheet("color: #ffcb92")
        layout.addWidget(self.status)
        layout.addWidget(self.message)
        split = QSplitter()
        left = QWidget()
        body = QVBoxLayout(left)
        body.setContentsMargins(0, 0, 0, 0)
        self.pages = QTabWidget()
        body.addWidget(self.pages)
        split.addWidget(left)
        preview_box = QWidget()
        preview_layout = QVBoxLayout(preview_box)
        self.preview = ScreenPreview()
        preview_layout.addWidget(self.preview, 1)
        self.preview_mode = QComboBox()
        for text, mode in (("ドラッグ：ゲームの範囲", "region"), ("クリック：自分", "player"),
                           ("クリック：集める物・目標", "goal"), ("クリック：避ける物", "danger"),
                           ("クリック：終了の目印", "stop")):
            self.preview_mode.addItem(text, mode)
        self.preview_mode.currentIndexChanged.connect(lambda: setattr(self.preview, "mode", self.preview_mode.currentData()))
        preview_layout.addWidget(self.preview_mode)
        self.button(preview_layout, "画像を大きくして教える", self.large_preview)
        self.button(preview_layout, "画像を更新（3秒後に取得）", lambda: self.later(self.freeze_preview, "画像取得を予約しました"))
        self.cue_label = QLabel("")
        self.cue_label.setWordWrap(True)
        preview_layout.addWidget(self.cue_label)
        self.button(preview_layout, "色の目印をすべて消す", self.clear_cues)
        split.addWidget(preview_box)
        split.setSizes([650, 360])
        layout.addWidget(split, 1)
        self.build_observation()
        self.build_game()
        self.build_demonstration()
        self.preview.marked.connect(self.mark_color)
        self.preview.region_selected.connect(lambda value: setattr(self.profile, "region", value))
        supervisor.problem.connect(self.message.setText)
        supervisor.changed.connect(self.refresh)
        supervisor.skill_saved.connect(self.skill_added)
        self.hide_avatar.toggled.connect(self.tab_changed)
        window.tabs.currentChanged.connect(self.tab_changed)
        self.load_games()
        self.display_profile()
        self.refresh()

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def page(self, title):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.pages.addTab(widget, title)
        return layout

    def build_observation(self):
        layout = self.page("見る・説明する")
        self.question = QLineEdit()
        self.question.setPlaceholderText("短い質問：この画面には何がある？")
        layout.addWidget(self.question)
        row = QHBoxLayout()
        self.observe_button = self.button(row, "今の画面を見てもらう", self.observe)
        self.button(row, "この説明を教える", self.teach)
        layout.addLayout(row)
        self.observation = QTextEdit()
        self.observation.setReadOnly(True)
        self.observation.setPlaceholderText("読み取った内容と、読み取りに失敗した理由をここに表示します。")
        layout.addWidget(self.observation, 1)

    def build_game(self):
        layout = self.page("ゲームを教える・試す")
        row = QHBoxLayout()
        self.game_list, self.preset = QComboBox(), QComboBox()
        self.game_list.setPlaceholderText("保存したゲームを選択")
        self.game_list.currentIndexChanged.connect(self.select_game)
        for label, value in (("slither.io（マウス）", "slither"), ("矢印キーの2Dゲーム", "arrows"), ("WASDの2Dゲーム", "wasd")):
            self.preset.addItem(label, value)
        row.addWidget(self.game_list, 1)
        row.addWidget(self.preset)
        self.button(row, "プリセットを読み込む", self.new_profile)
        layout.addLayout(row)
        self.game_name = QLineEdit()
        self.rules = QTextEdit()
        self.rules.setMaximumHeight(75)
        self.rules.setPlaceholderText("目的、画面の見方、キーの役割、終了条件を教えてください。")
        layout.addWidget(self.game_name)
        layout.addWidget(self.rules)
        row = QHBoxLayout()
        self.controller, self.steering = QComboBox(), QComboBox()
        self.controller.addItem("色の目印を追う（速い2D向け）", "colors")
        self.controller.addItem("画像AIでルールを考える（低速向け）", "vision")
        self.steering.addItem("マウスで方向を決める", "mouse")
        self.steering.addItem("キーで方向を決める", "keys")
        self.centered = QCheckBox("自分の頭は画面の同じ位置")
        for widget in (self.controller, self.steering, self.centered):
            row.addWidget(widget)
        layout.addLayout(row)
        self.bindings = QTableWidget(0, 4)
        self.bindings.setHorizontalHeaderLabels(["操作の名前（上・下・左・右など）", "キー（例：W+SPACE）", "マウス（left等）", "最長 ms"])
        self.bindings.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.bindings.setMinimumHeight(130)
        self.bindings.setMaximumHeight(190)
        layout.addWidget(self.bindings)
        row = QHBoxLayout()
        self.button(row, "操作を追加", self.add_binding)
        self.button(row, "選んだ操作を削除", lambda: self.bindings.removeRow(self.bindings.currentRow()))
        self.button(row, "教えた内容を保存", self.save_profile)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.button(row, "ゲームの実演を記録（3秒後）", self.record_game)
        self.button(row, "記録を終了して覚える", self.finish_record)
        self.button(row, "操作を1回試す（3秒後）", self.test_binding)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.seconds = QSpinBox()
        self.seconds.setRange(1, 300)
        self.seconds.setValue(5)
        row.addWidget(QLabel("試す時間（秒）"))
        row.addWidget(self.seconds)
        self.play_button = self.button(row, "プレイを試す（3秒後）", self.play)
        layout.addLayout(row)
        note = QLabel("右の画像でゲームの範囲と目印を教えます。色の目印モードは教えた色だけを区別します。\n"
                      "画像AIモードはルールの文章と実演も参照します。F8で停止・キーを解放します。")
        note.setWordWrap(True)
        layout.addWidget(note)

    def build_demonstration(self):
        layout = self.page("一般のPC操作・実演")
        self.task = QLineEdit()
        self.task.setPlaceholderText("頼む操作：この入力欄に hello ESI と入力して")
        layout.addWidget(self.task)
        self.button(layout, "この操作を頼む（3秒後）", lambda: self.later(
            lambda: self.command("task", {"goal": self.task.text()}), "操作を予約しました"))
        self.skill_name, self.skill_goal = QLineEdit(), QLineEdit()
        self.skill_name.setPlaceholderText("実演の名前（例：メモを書く）")
        self.skill_goal.setPlaceholderText("何を達成する操作か")
        layout.addWidget(self.skill_name)
        layout.addWidget(self.skill_goal)
        row = QHBoxLayout()
        self.button(row, "実演の記録を開始（3秒後）", self.record)
        self.button(row, "記録を終了して覚える", self.finish_record)
        self.button(row, "記録を破棄", lambda: self.supervisor.stop_recording(False))
        layout.addLayout(row)
        self.skills = QListWidget()
        self.skills.setMaximumHeight(100)
        self.skills.currentRowChanged.connect(self.selected_skill)
        layout.addWidget(self.skills)
        self.allow_practice = QCheckBox("選んだ実演は、自分で選んで練習してよい")
        self.allow_practice.toggled.connect(self.practice_changed)
        layout.addWidget(self.allow_practice)
        row = QHBoxLayout()
        self.button(row, "選んだ実演を再現（3秒後）", self.replay)
        self.button(row, "選んだ実演を削除", self.delete_skill)
        layout.addLayout(row)
        layout.addStretch()

    def help(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("画面・ゲーム操作の教え方")
        dialog.resize(760, 650)
        layout = QVBoxLayout(dialog)
        text = QTextBrowser()
        text.setMarkdown(HELP)
        layout.addWidget(text)
        self.button(layout, "閉じる", dialog.accept)
        dialog.exec()

    def open_wide(self):
        from PySide6.QtWidgets import QScrollArea
        if self.expanded_dialog:
            self.expanded_dialog.raise_()
            return
        original = self.window.computer_scroll
        original.takeWidget()
        dialog = QDialog(self.window)
        dialog.setWindowTitle("ESI — 画面を見る・操作を教える・ゲームを試す")
        dialog.resize(1280, 850)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self)
        layout.addWidget(scroll)
        self.expanded_dialog = dialog
        def restore():
            scroll.takeWidget()
            original.setWidget(self)
            self.expanded_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(restore)
        dialog.show()
        self.tab_changed()

    def close_wide(self):
        if self.expanded_dialog:
            self.expanded_dialog.close()

    def large_preview(self):
        if not self.preview.frame:
            self.message.setText("対象の画像を取得してから開いてください。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("ゲームの範囲・色を教える")
        dialog.resize(1200, 800)
        layout = QVBoxLayout(dialog)
        modes = QComboBox()
        for index in range(self.preview_mode.count()):
            modes.addItem(self.preview_mode.itemText(index), self.preview_mode.itemData(index))
        modes.setCurrentIndex(self.preview_mode.currentIndex())
        layout.addWidget(modes)
        preview = ScreenPreview()
        preview.setMaximumHeight(1400)
        preview.show_frame(self.preview.frame)
        preview.region, preview.cues, preview.mode = self.preview.region, self.profile.cues, modes.currentData()
        modes.currentIndexChanged.connect(lambda: setattr(preview, "mode", modes.currentData()))
        preview.marked.connect(self.mark_color)
        preview.marked.connect(lambda *args: preview.update())
        def region_selected(value):
            self.profile.region = self.preview.region = value
            self.preview.update()
        preview.region_selected.connect(region_selected)
        layout.addWidget(preview, 1)
        self.button(layout, "閉じる", dialog.accept)
        dialog.exec()

    def tab_changed(self, *args):
        avatar = getattr(getattr(self.window, "desktop", None), "avatar", None)
        scroll = getattr(self.window, "computer_scroll", None)
        if avatar and scroll:
            active = self.window.tabs.currentWidget() is scroll or bool(self.expanded_dialog)
            avatar.setVisible(self.window.desktop.preferences.avatar_visible and not (active and self.hide_avatar.isChecked()))

    def refresh_screens(self):
        selected = self.screens.currentData()
        self.screens.clear()
        for screen in self.window.screen().virtualSiblings():
            g = screen.geometry()
            self.screens.addItem(f"{screen.name()}（{g.width()}×{g.height()}）", screen.name())
        index = self.screens.findData(selected)
        if index >= 0:
            self.screens.setCurrentIndex(index)

    def apply(self):
        try:
            self.countdown_token += 1
            self.supervisor.configure(screen=self.screens.currentData() or "", observe=True,
                control=self.allow_control.isChecked(), demonstrate=self.allow_record.isChecked(),
                move_avatar=self.allow_move.isChecked())
            self.preview.frame = None
            self.message.setText("共有を開始しました。ゲームや特定アプリを操作するときは対象ウィンドウを選んでください。")
        except Exception as exc:
            self.message.setText(str(exc))

    def later(self, callback, label):
        self.countdown_token += 1
        token = self.countdown_token
        self.message.setText(label+"。3秒以内に対象アプリへ切り替え、ゲーム内をクリックしてください。")
        def run():
            if token != self.countdown_token:
                return
            try:
                callback()
            except Exception as exc:
                self.message.setText(str(exc))
        QTimer.singleShot(3000, run)

    def choose_target(self):
        def choose():
            current = self.supervisor.hub.native.foreground()
            if current.pid == os.getpid() or int(self.window.winId()) == current.handle:
                raise ValueError("ESI自身が選ばれました。もう一度押し、ゲームへ切り替えてください。")
            if current.bounds:
                from esi04.computer_gui import physical_screen
                def overlap(screen):
                    area, target = physical_screen(screen), current.bounds
                    return max(0, min(area.x+area.width, target.x+target.width)-max(area.x, target.x))*max(
                        0, min(area.y+area.height, target.y+target.height)-max(area.y, target.y))
                screen = max(self.window.screen().virtualSiblings(), key=overlap)
                self.screens.setCurrentIndex(self.screens.findData(screen.name()))
            self.supervisor.configure(screen=self.screens.currentData() or "", observe=True,
                control=self.allow_control.isChecked(), demonstrate=self.allow_record.isChecked(),
                move_avatar=self.allow_move.isChecked(), application=current.application, pid=current.pid, handle=current.handle)
            self.preview.frame = None
            self.message.setText("対象を選びました。ESIへ戻ると最後に取得した対象画像を表示します。")
            QTimer.singleShot(700, self.freeze_preview)
        self.later(choose, "対象の選択を予約しました")

    def clear_target(self):
        self.apply()

    def stop(self):
        self.countdown_token += 1
        self.supervisor.revoke()
        if self.window.runtime:
            self.window.send("stop_computer", None)
        self.message.setText("画面共有・実演・PC操作を停止し、ESIが押していたキーとボタンを離しました。")

    def command(self, operation, values):
        if not self.window.runtime:
            self.message.setText("本体上部の開始ボタンでESIを開始してから使ってください。")
            return False
        if self.supervisor.recording:
            self.message.setText("実演の記録を終了してから、観察や操作を頼んでください。")
            return False
        if self.window.send("computer", (operation, values)) is False:
            self.message.setText("処理を受け付けられませんでした。開始状態を確認して、もう一度試してください。")
            return False
        self.started_at = time.monotonic()
        self.last_error = None
        self.message.setText("要求を送りました。対象アプリを前面にしてください。")
        return True

    def observe(self):
        def callback():
            return self.command("observe", {"question": self.question.text() or "この画面には何が見える？"})
        if self.supervisor.hub.grant.handle:
            self.later(callback, "画面の読み取りを予約しました")
        else:
            callback()

    def freeze_preview(self):
        frame = self.supervisor.hub.latest
        if frame:
            self.preview.show_frame(frame)
            self.message.setText("対象の取得画像を表示しました。画像内をドラッグ、または目印をクリックできます。")
        else:
            self.message.setText("画像を待っています。共有を開始し、選んだ対象アプリを前面にしてから戻ってください。")

    def teach(self):
        text, frame = self.question.text().strip(), self.preview.frame or self.supervisor.hub.latest
        if not text or frame is None:
            self.message.setText("画面を取得し、教える説明を入力してください。")
            return
        entry = ScreenTeaching(id=uid(), frame_id=frame.id, frame_sha256=frame.sha256, user_text=text, observed_at=utc())
        self.supervisor.hub.memory.record("teachings", entry)
        self.command("teaching", entry.model_dump())

    def display_profile(self):
        profile = self.profile
        self.game_name.setText(profile.name)
        self.rules.setPlainText(profile.instructions)
        self.controller.setCurrentIndex(self.controller.findData(profile.controller))
        self.steering.setCurrentIndex(self.steering.findData(profile.steering))
        self.centered.setChecked(profile.player_centered)
        self.bindings.setRowCount(0)
        for binding in profile.bindings:
            self.add_binding(binding)
        self.preview.region, self.preview.cues = list(profile.region), list(profile.cues)
        self.update_cues()

    def add_binding(self, binding=None):
        binding = binding if isinstance(binding, Binding) else Binding(name="操作", keys=["SPACE"])
        row = self.bindings.rowCount()
        self.bindings.insertRow(row)
        for column, value in enumerate((binding.name, "+".join(binding.keys), binding.button, str(binding.milliseconds))):
            self.bindings.setItem(row, column, QTableWidgetItem(value))

    def read_profile(self):
        bindings = []
        for row in range(self.bindings.rowCount()):
            values = [self.bindings.item(row, c).text().strip() if self.bindings.item(row, c) else "" for c in range(4)]
            bindings.append(Binding(name=values[0], keys=[k.strip().upper() for k in values[1].split("+") if k.strip()],
                                    button=values[2].lower(), milliseconds=int(values[3] or "180")))
        self.profile = GameProfile.model_validate({**self.profile.model_dump(), "name": self.game_name.text().strip(),
            "instructions": self.rules.toPlainText().strip(), "bindings": bindings,
            "controller": self.controller.currentData(), "steering": self.steering.currentData(),
            "player_centered": self.centered.isChecked(), "region": self.preview.region,
            "application": self.supervisor.hub.grant.application or self.profile.application})
        return self.profile

    def save_profile(self):
        try:
            self.supervisor.hub.memory.save_game(self.read_profile())
            self.load_games()
            self.message.setText("教えた操作・ルール・色の目印を、このセーブに保存しました。")
            return True
        except Exception as exc:
            self.message.setText(str(exc))
            return False

    def load_games(self):
        self.game_list.blockSignals(True)
        self.game_list.clear()
        for game in self.supervisor.hub.memory.games():
            self.game_list.addItem(game.name, game.id)
        index = self.game_list.findData(self.profile.id)
        self.game_list.setCurrentIndex(index)
        self.game_list.blockSignals(False)

    def select_game(self):
        game = next((g for g in self.supervisor.hub.memory.games() if g.id == self.game_list.currentData()), None)
        if game:
            self.profile = game
            self.display_profile()

    def new_profile(self):
        self.profile = presets(self.preset.currentData())
        self.game_list.setCurrentIndex(-1)
        self.display_profile()
        self.message.setText("操作表を読み込みました。ゲームの範囲と、自分・目標・危険の目印を右の画像で教えてください。")

    def mark_color(self, role, point):
        try:
            cue = teach_color(self.preview.frame, role, point)
            if len(self.profile.cues) >= 24:
                raise ValueError("色の目印は24個までです。不要な目印を消してください。")
            self.profile.cues.append(cue)
            if role == "player":
                self.profile.player_point = list(point)
            self.preview.cues = self.profile.cues
            self.update_cues()
            self.message.setText("目印を追加しました。教えた内容を保存してください。")
        except Exception as exc:
            self.message.setText(str(exc))

    def clear_cues(self):
        self.profile.cues.clear()
        self.preview.cues = []
        self.update_cues()

    def update_cues(self):
        self.cue_label.setText(" / ".join(label+str(sum(c.role == role for c in self.profile.cues)) for role, label in
            (("player", "自分 "), ("goal", "目標 "), ("danger", "危険 "), ("stop", "終了 "))))
        self.preview.update()

    def play(self):
        if self.save_profile():
            self.later(lambda: self.command("game", {"id": self.profile.id, "seconds": self.seconds.value()}), "プレイ開始を予約しました")

    def test_binding(self):
        row = self.bindings.currentRow()
        if row < 0:
            self.message.setText("操作表で試す操作の行を選んでください。")
        elif self.save_profile():
            name = self.profile.bindings[row].name
            self.later(lambda: self.command("test_input", {"id": self.profile.id, "binding": name}), "1回の入力を予約しました")

    def record_game(self):
        if self.save_profile():
            self.later(lambda: self.supervisor.start_game_recording(self.profile), "ゲームの実演を予約しました")

    def record(self):
        self.later(lambda: self.supervisor.start_recording(self.skill_name.text(), self.skill_goal.text()), "実演の記録を予約しました")

    def finish_record(self):
        try:
            self.supervisor.stop_recording()
            self.load_games()
            self.message.setText("実演を保存しました。次に対象アプリへ切り替えて再現・試行できます。")
        except Exception as exc:
            self.message.setText(str(exc))

    def skill_added(self, identifier):
        self.loaded_skills = None
        self.refresh()
        if self.window.runtime:
            self.window.send("computer", ("skill_learned", {"id": identifier}))

    def chosen_skill(self):
        item = self.skills.currentItem()
        return next((s for s in self.supervisor.hub.memory.skills() if item and s.id == item.data(Qt.ItemDataRole.UserRole)), None)

    def replay(self):
        skill = self.chosen_skill()
        if skill:
            self.later(lambda: self.command("replay", {"id": skill.id}), "実演の再現を予約しました")

    def selected_skill(self):
        skill = self.chosen_skill()
        self.allow_practice.blockSignals(True)
        self.allow_practice.setChecked(bool(skill and skill.practice_allowed))
        self.allow_practice.blockSignals(False)

    def practice_changed(self, value):
        skill = self.chosen_skill()
        if skill:
            skill.practice_allowed = bool(value)
            self.supervisor.hub.memory.save_skill(skill)

    def delete_skill(self):
        skill = self.chosen_skill()
        if skill:
            self.stop()
            self.supervisor.hub.memory.delete_skill(skill.id)
            self.loaded_skills = None
            self.refresh()

    def refresh(self):
        grant, frame = self.supervisor.hub.grant, self.supervisor.hub.latest
        if grant.generation != self.displayed_generation:
            if self.displayed_generation is not None:
                self.countdown_token += 1
            self.displayed_generation = grant.generation
            for widget, value in ((self.allow_observe, grant.observe), (self.allow_control, grant.control), (self.allow_record, grant.demonstrate)):
                widget.setChecked(value)
        target = (frame.foreground.title[:90]+" / "+grant.application
                  if frame and grant.handle else grant.application or "モニター全体")
        self.status.setText(f"対象：{target} / 画面：{grant.screen} / 操作：{'許可' if grant.control else '停止'}"
            + (" / 実演を記録中" if self.supervisor.recording else "") if grant.observe else "画面共有・PC操作：停止")
        if frame and self.preview.frame is None:
            self.preview.show_frame(frame)
        if self.loaded_skills is None:
            self.skills.clear()
            skills = self.supervisor.hub.memory.skills()
            for skill in skills:
                self.skills.addItem(skill.name+f"（{len(skill.steps)}操作）")
                self.skills.item(self.skills.count()-1).setData(Qt.ItemDataRole.UserRole, skill.id)
            self.loaded_skills = len(skills)

    def runtime_status(self, status):
        phase = status.get("computer_phase", "waiting")
        busy = phase in {"observing", "operating", "playing"}
        self.observe_button.setEnabled(not busy)
        self.play_button.setEnabled(not busy)
        if busy:
            seconds = round(time.monotonic()-self.started_at) if self.started_at else 0
            backend = status.get("vision_backend", {}).get("device", "確認中")
            step = status.get("game_step", {})
            self.message.setText((f"ゲームを試しています：{step.get('count', 0)}判断 / {step.get('reason', '')}"
                if phase == "playing" else f"{'画面を読み取り中' if phase == 'observing' else 'PCを操作中'}：{seconds}秒 / 画像AI：{backend}")
                + "。F8または「すべて停止」で中止できます。")
        elif status.get("computer_error") and status["computer_error"] != self.last_error:
            self.message.setText(status["computer_error"])
            self.last_error = status["computer_error"]
        if not status.get("computer_error"):
            self.last_error = None
        observation = status.get("screen_observation")
        if observation and observation.get("id") != self.last_observation:
            self.last_observation = observation.get("id")
            result = observation["result"]
            self.observation.setPlainText(result["description"]+"\n"+"\n".join(result["visible_text"])
                + ("\n画像の読み取りは未確認の推定を含みます。" if result["uncertain"] else ""))
            if not busy and not status.get("computer_error"):
                self.message.setText("画面の読み取りが完了しました。画像AI："+str(status.get("vision_backend", {}).get("device", "確認中")))
        result = status.get("computer_result")
        if not busy and result and result != getattr(self, "last_game_result", None):
            self.last_game_result = result
            self.message.setText(result["summary"])
