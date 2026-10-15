"""Visible save identity, earlier transcript pages and source-preserving recovery."""

import sqlite3
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from esi04.save_history import discover_saves, history_page, save_summary


def choose_recovery(parent, root, current):
    entries = discover_saves(root, current)
    dialog = QDialog(parent)
    dialog.setWindowTitle("以前のセーブ・会話履歴を探す")
    dialog.resize(820, 440)
    layout = QVBoxLayout(dialog)
    label = QLabel("現在のセーブ："+str(current)+"\n以前のセーブを選べます。旧版は新しいファイルへコピーし、現在のセーブも残します。")
    label.setWordWrap(True)
    layout.addWidget(label)
    choices = QListWidget()
    for entry in entries:
        choices.addItem(f"{entry['release']} / {entry['name']} / 会話 {entry['turns']}件\n{entry['path']}")
    layout.addWidget(choices)
    if not entries:
        label.setText(label.text()+"\nこのフォルダー内には見つかりませんでした。「セーブを開く」で元フォルダーのセーブを指定してください。")
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(False)
    choices.currentRowChanged.connect(lambda row: buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(row >= 0))
    choices.itemDoubleClicked.connect(lambda _: dialog.accept())
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return entries[choices.currentRow()]["path"] if dialog.exec() and choices.currentRow() >= 0 else None


class HistoryPanel(QWidget):
    def __init__(self, main):
        super().__init__(main)
        self.main, self.before = main, None
        layout = QVBoxLayout(self)
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        layout.addWidget(self.identity)
        recover = QPushButton("以前のセーブ・会話履歴を探す")
        recover.clicked.connect(main.recover_history)
        layout.addWidget(recover)
        refresh = QPushButton("最新の会話を表示")
        refresh.clicked.connect(self.reload)
        layout.addWidget(refresh)
        self.text = QTextBrowser()
        layout.addWidget(self.text, 1)
        self.older = QPushButton("さらに前の会話を表示")
        self.older.clicked.connect(self.load_page)
        layout.addWidget(self.older)

    def reload(self):
        self.before = None
        self.text.clear()
        self.load_page()

    def load_page(self):
        path = Path(self.main.session.store.path)
        try:
            summary = save_summary(path)
            self.identity.setText(f"保存先：{path.resolve()}\n{summary['release']} / {summary['name']} / 保存済みの会話 {summary['turns']}件\n会話タブは直近12件を表示します。この画面で以前の記録も読めます。")
            rows = history_page(path, self.before)
            if not rows:
                self.older.setEnabled(False)
                if not self.text.toPlainText():
                    self.text.setPlainText("このセーブには会話の記録がありません。以前のセーブも確認できます。")
                return
            self.before = rows[-1][0]
            lines = []
            for _, turn in reversed(rows):
                lines.extend(["あなた："+(turn["input"] or "（聞き取りなし）"),
                              "ESI："+turn["reply"], ""])
            previous = self.text.toPlainText()
            self.text.setPlainText("\n\n".join(lines)+(previous and "\n\n"+previous))
            self.older.setEnabled(True)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.identity.setText("履歴を開けません。元データは保持しています："+str(exc))
