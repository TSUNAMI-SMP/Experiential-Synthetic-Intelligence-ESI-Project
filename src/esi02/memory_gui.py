"""Visible provenance and explicit memory management for the person using ESI."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from esi02.memory import TRUST

KINDS = {"fact": "事実・自己紹介", "preference": "好み", "topic": "未解決の話題"}
STATES = {"active": "現在", "superseded": "訂正前", "resolved": "解決済み"}


class MemoryDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.selected_id, self.last_signature = None, None
        self.setWindowTitle("ESI-02の記憶を見る・直す")
        self.resize(930, 650)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "話した内容を記憶に整理します。「本人の発言」は世界での真偽を検証した意味ではありません。\nAIの推測は別に表示します。訂正・削除すると、古い会話を次の返答へ渡すのを止めます。会話の記録や元のセーブは残ります。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("名前・紅茶などを検索")
        self.query.textChanged.connect(self.refresh)
        row.addWidget(self.query, 1)
        self.include_old = QCheckBox("訂正前・解決済みも表示")
        self.include_old.toggled.connect(self.refresh)
        row.addWidget(self.include_old)
        layout.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["種類", "項目", "内容", "確認状態", "状態"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.select)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.quote = QTextEdit()
        self.quote.setReadOnly(True)
        self.quote.setMaximumHeight(95)
        layout.addWidget(self.quote)
        editor = QHBoxLayout()
        self.kind = QComboBox()
        for key, label in KINDS.items():
            self.kind.addItem(label, key)
        editor.addWidget(self.kind)
        self.label = QLineEdit()
        self.label.setPlaceholderText("項目（例：名前）")
        self.label.setMaxLength(60)
        editor.addWidget(self.label)
        self.value = QLineEdit()
        self.value.setPlaceholderText("内容（例：次郎）")
        self.value.setMaxLength(120)
        editor.addWidget(self.value, 1)
        layout.addLayout(editor)
        actions = QHBoxLayout()
        self.confirm = QPushButton("訂正・確認して保存")
        self.confirm.clicked.connect(self.edit)
        self.delete_button = QPushButton("この項目を記憶から削除")
        self.delete_button.clicked.connect(self.delete)
        self.resolve = QPushButton("話題を解決済みにする")
        self.resolve.clicked.connect(lambda: self.edit(resolved=True))
        add = QPushButton("新しい記憶を追加")
        add.clicked.connect(self.add)
        for widget in (self.confirm, self.delete_button, self.resolve, add):
            actions.addWidget(widget)
        layout.addLayout(actions)
        row = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        retry = QPushButton("整理に失敗した記憶を再試行")
        retry.clicked.connect(lambda: self.window.memory_action("retry", {}))
        row.addWidget(retry)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()
        self.select()

    def refresh(self):
        memory = self.window.session.memory
        signature = (id(memory), memory.revision, self.query.text(), self.include_old.isChecked())
        if self.last_signature != signature:
            entries = memory.search(self.query.text(), self.include_old.isChecked(), limit=1000)
            self.table.blockSignals(True)
            self.table.setRowCount(len(entries))
            selected = None
            for row, item in enumerate(entries):
                values = [
                    KINDS[item["kind"]],
                    item["label"],
                    item["value"],
                    TRUST[item["trust"]],
                    STATES[item["status"]],
                ]
                for col, text in enumerate(values):
                    cell = QTableWidgetItem(text)
                    if col == 0:
                        cell.setData(Qt.UserRole, item)
                    self.table.setItem(row, col, cell)
                if item["id"] == self.selected_id:
                    selected = row
            if selected is not None:
                self.table.selectRow(selected)
            self.table.blockSignals(False)
            self.table.resizeColumnsToContents()
            self.last_signature = signature
            self.select()
        pending = memory.pending()
        self.status.setText(
            f"記憶 {len(memory.entries())}件 / 整理待ち {len(pending)}件 / 失敗 {sum(m['attempts'] >= 2 for m in pending)}件\n"
            + (
                self.window.session.view()["status"].get("memory_error")
                or self.window.ui_error
                or "新しい会話を優先し、返答が終わってから記憶を整理します。"
            )
        )

    def select(self):
        row = self.table.currentRow()
        cell = self.table.item(row, 0) if row >= 0 else None
        item = cell.data(Qt.UserRole) if cell else None
        self.selected_id = item["id"] if item else None
        active = bool(item and item["status"] == "active")
        self.confirm.setEnabled(active)
        self.delete_button.setEnabled(item is not None)
        self.resolve.setEnabled(active and item["kind"] == "topic")
        if item:
            self.kind.setCurrentIndex(self.kind.findData(item["kind"]))
            self.label.setText(item["label"])
            self.value.setText(item["value"])
            self.quote.setPlainText(
                f"元の発言：{item['quote'] or '記憶画面で登録'}\n入力元：{item['source']} / 記録日時：{item['created_at']}\n"
                + (
                    "内容は画面で訂正されています。元の発言は訂正後の内容の証拠ではありません。"
                    if item.get("edited_in_ui")
                    else ""
                )
            )
        else:
            self.quote.clear()

    def edit(self, checked=False, resolved=False):
        if self.selected_id:
            self.window.memory_action(
                "edit", {"id": self.selected_id, "value": self.value.text(), "resolved": resolved}
            )
            self.refresh()

    def delete(self):
        if self.selected_id:
            self.window.memory_action("delete", {"id": self.selected_id})
            self.refresh()

    def add(self):
        self.window.memory_action(
            "add", {"kind": self.kind.currentData(), "label": self.label.text(), "value": self.value.text()}
        )
        self.refresh()
