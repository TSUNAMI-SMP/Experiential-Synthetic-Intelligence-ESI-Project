"""Needs, optional requests and visible observed learning, without matrix internals."""

from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QProgressBar, QTextEdit, QVBoxLayout, QWidget

NEEDS = {
    "maintenance": "身体を維持したい",
    "comfort": "快適にしたい",
    "rest": "休みたい",
    "curiosity": "周りを調べたい",
    "social": "話したい",
}
MODULATORS = {
    "dopamine": "意欲・予測のずれ（ドーパミン相当）",
    "serotonin": "安定（セロトニン相当）",
    "noradrenaline": "警戒（ノルアドレナリン相当）",
    "oxytocin": "対人への接近（オキシトシン相当）",
}
STAGES = {
    "running": "行動中",
    "awaiting": "対応を待っています",
    "complete": "結果を確認",
    "unknown": "結果は不明",
    "declined": "お願いを控えます",
    "cancelled": "終了・学習しません",
}


class MotivationPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("自発的な行動・お願いを使う")
        self.enabled.toggled.connect(lambda checked: self.window.motivation_action("enabled", checked))
        layout.addWidget(self.enabled)
        note = QLabel(
            "仮想身体から欲求を計算します。お願いへの対応は任意です。断ったり後にした場合は、同じお願いを15分間控えます。\n記憶と、行動後の結果の見通しを更新します。結果を確認できない行動では学習しません。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.needs = self.bars(layout, NEEDS)
        layout.addWidget(QLabel("仮想の調整値（生化学の測定や、実際の感情の証明ではありません）"))
        self.modulators = self.bars(layout, MODULATORS)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.records = QTextEdit()
        self.records.setReadOnly(True)
        layout.addWidget(self.records, 1)

    def bars(self, layout, labels):
        grid, bars = QGridLayout(), {}
        for row, (key, label) in enumerate(labels.items()):
            grid.addWidget(QLabel(label), row, 0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            grid.addWidget(bar, row, 1)
            bars[key] = bar
        layout.addLayout(grid)
        return bars

    def refresh(self, view):
        self.enabled.blockSignals(True)
        self.enabled.setChecked(view["enabled"])
        self.enabled.blockSignals(False)
        for key, bar in self.needs.items():
            bar.setValue(round(view["needs"][key] * 100))
        for key, bar in self.modulators.items():
            bar.setValue(round(view["modulators"][key] * 100))
        active = view["active_action"]
        self.summary.setText(
            ("いま選んだ行動：" + active["label"] if active else "いまは身体の様子を見ています")
            + f" / 観測した結果から学習した経験 {sum(view['counts'].values())}回"
        )
        entries = []
        for action in view["actions"]:
            result = f"報酬 {action['reward']:+.3f}" if action["reward"] is not None else "報酬は未確定"
            entries.append(
                f"{action['label']} → {STAGES[action['stage']]} / {result}"
                + (" / 学習しました" if action["learned"] else "")
                + ("\n" + action["reason"] if action["reason"] else "")
            )
        content = (
            "\n\n".join(entries)
            or "まだ自発的な行動の記録はありません。開始すると、時間と身体の状態に応じて行動します。"
        )
        if content != self.records.toPlainText():
            self.records.setPlainText(content)
            self.records.verticalScrollBar().setValue(self.records.verticalScrollBar().maximum())
