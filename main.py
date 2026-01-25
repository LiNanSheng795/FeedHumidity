import sys
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QTableWidget, QTableWidgetItem, QSpinBox, QMessageBox
)

import pyqtgraph as pg

def resource_path(relative_path: str) -> str:
    # PyInstaller 单文件运行时会把资源解压到临时目录：sys._MEIPASS
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

# 原始数据报头
HEADER = ["01", "03", "04"]


@dataclass
class ParsedRecord:
    idx: int
    ts: float
    hex32: str
    dec: int


class StreamParser:
    """
    增量解析：输入一段文本（可能包含半条记录），输出解析到的记录 + 剩余未完成 token。
    """
    def __init__(self):
        self._tokens: List[str] = []

    @staticmethod
    def _is_hex_byte(tok: str) -> bool:
        if len(tok) != 2:
            return False
        try:
            int(tok, 16)
            return True
        except ValueError:
            return False

    def feed_text(self, text: str) -> List[Tuple[str, int]]:
        # 只保留像 "8D" 这种两位 token，其他忽略（也能抗 txt 中混入杂字符/换行）
        raw = text.replace("\r", " ").replace("\n", " ").split()
        for tok in raw:
            t = tok.upper()
            if self._is_hex_byte(t):
                self._tokens.append(t)

        out: List[Tuple[str, int]] = []

        i = 0
        # 每条：01 03 04 + 6 bytes (LL ll HH hh YY YY) = 9 tokens total
        needed_after_header = 6
        total_len = 3 + needed_after_header

        while i + total_len <= len(self._tokens):
            if self._tokens[i:i+3] != HEADER:
                i += 1
                continue

            # token 顺序：LL ll HH hh YY YY
            ll = self._tokens[i+3]
            l  = self._tokens[i+4]
            hh = self._tokens[i+5]
            h  = self._tokens[i+6]
            # yy1 = self._tokens[i+7]
            # yy2 = self._tokens[i+8]

            # 你要的：HH hh LL ll -> 32bit
            value = (int(hh, 16) << 24) | (int(h, 16) << 16) | (int(ll, 16) << 8) | int(l, 16)
            hex32 = f"{value:08X}"
            out.append((hex32, value))

            i += total_len

        # 丢掉已消费 token，保留尾部未完成部分
        if i > 0:
            self._tokens = self._tokens[i:]

        return out


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("力源饲料水分监测仪")    # 窗口名称
        icon_path = resource_path("icon.ico")
        self.setWindowIcon(QIcon(icon_path))
        self.resize(980, 680)    # 默认窗口大小

        self.file_path: Optional[str] = None
        self.file_pos: int = 0  # 文件指针初始化为0——意思是从头开始读文件
        self.parser = StreamParser()

        self.records: List[ParsedRecord] = []
        self.next_idx = 1

        # --- UI ---
        root = QVBoxLayout(self)

        # Top bar
        top = QHBoxLayout()
        self.path_label = QLabel("文件：未选择")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.led = QLabel()
        self.led.setFixedSize(14, 14)    # 圆点大小
        self.led.setToolTip("状态指示灯")
        self.set_led("gray")
        self.status_label = QLabel("状态：停止")
        self.latest_label = QLabel("最新值：—")
        self.latest_label.setStyleSheet("font-size: 22px; font-weight: 700; color: blue")

        btn_pick = QPushButton("选择文件")
        btn_pick.clicked.connect(self.pick_file)

        self.btn_toggle = QPushButton("开始监控")
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.clicked.connect(self.toggle_monitor)

        top.addWidget(self.path_label, 4)
        top.addWidget(self.led)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.latest_label, 2)
        top.addWidget(btn_pick)
        top.addWidget(self.btn_toggle)
        root.addLayout(top)

        # Plot
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel("left", "Value (dec)")
        self.plot.setLabel("bottom", "Index")
        self.curve = self.plot.plot([], [])
        root.addWidget(self.plot, 3)

        # Controls + Table
        mid = QHBoxLayout()

        left = QVBoxLayout()
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("曲线显示最近 N 条："))
        self.spin_n = QSpinBox()
        self.spin_n.setRange(50, 200000)
        self.spin_n.setValue(2000)
        ctrl.addWidget(self.spin_n)
        self.btn_export = QPushButton("导出CSV")
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_clear = QPushButton("清空历史")
        self.btn_clear.clicked.connect(self.clear_history)
        ctrl.addWidget(self.btn_export)
        ctrl.addWidget(self.btn_clear)
        ctrl.addStretch(1)
        left.addLayout(ctrl)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Idx", "Time", "Hex(32bit)", "Dec"])
        self.table.setColumnWidth(0, 60)    # idx列宽度
        self.table.setColumnWidth(1, 120)    # time列宽度
        self.table.setColumnWidth(2, 110)    # hex32列宽度
        self.table.setColumnWidth(3, 100)    # dec列宽度
        self.table.verticalHeader().setVisible(False)    # 隐藏左侧行号
        self.table.horizontalHeader().setStretchLastSection(True)    # 最后一列自动填满剩余空间
        left.addWidget(self.table, 1)

        mid.addLayout(left, 1)
        root.addLayout(mid, 3)

        # Timer
        self.timer = QTimer(self)
        self.timer.setInterval(300)  # 300ms
        self.timer.timeout.connect(self.poll_file)

        self.monitoring = False

    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择传感器 txt", "", "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        self.reset_session()
        self.file_path = path
        self.path_label.setText(f"文件：{path}")
        self.btn_toggle.setEnabled(True)

        # 重置读指针：默认从文件末尾开始“只看新增”
        try:
            # self.file_pos = os.path.getsize(path)
            self.file_pos = 0
        except OSError:
            self.file_pos = 0

        self.status_label.setText("状态：就绪（从末尾开始监控）")
        self.latest_label.setText("最新值：—")
        self.set_led("yellow")

    def toggle_monitor(self):
        if not self.file_path:
            return
        self.monitoring = not self.monitoring
        if self.monitoring:
            self.timer.start()
            self.btn_toggle.setText("暂停")
            self.status_label.setText("状态：监控中")
            self.set_led("green")
        else:
            self.timer.stop()
            self.btn_toggle.setText("开始监控")
            self.status_label.setText("状态：暂停")
            self.set_led("yellow")

    def poll_file(self):
        if not self.file_path:
            return

        try:
            size = os.path.getsize(self.file_path)
        except OSError:
            self.status_label.setText("状态：文件不可读")
            self.set_led("red")
            return

        # 如果文件被重写/截断：从头重新读
        if size < self.file_pos:
            self.file_pos = 0
            self.parser = StreamParser()

        if size == self.file_pos:
            return

        try:
            with open(self.file_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.file_pos)
                new_text = f.read()
                self.file_pos = f.tell()
        except OSError:
            self.status_label.setText("状态：读取失败")
            self.set_led("red")
            return

        parsed = self.parser.feed_text(new_text)
        if not parsed:
            return

        now = time.time()
        for hex32, dec in parsed:
            rec = ParsedRecord(idx=self.next_idx, ts=now, hex32=hex32, dec=dec)
            self.records.append(rec)
            self.append_row(rec)
            self.next_idx += 1
            self.latest_label.setText(f"最新值：{dec}")
            if self.monitoring:
                self.set_led("green")

        self.refresh_plot()

    def append_row(self, rec: ParsedRecord):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(rec.idx)))
        t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.ts))
        self.table.setItem(row, 1, QTableWidgetItem(t_str))
        self.table.setItem(row, 2, QTableWidgetItem(rec.hex32))
        self.table.setItem(row, 3, QTableWidgetItem(str(rec.dec)))

        # 自动滚到最新
        self.table.scrollToBottom()

    def refresh_plot(self):
        n = int(self.spin_n.value())
        data = self.records[-n:] if len(self.records) > n else self.records
        x = [r.idx for r in data]
        y = [r.dec for r in data]
        self.curve.setData(x, y)

    def clear_history(self):
        self.records.clear()
        self.next_idx = 1
        self.table.setRowCount(0)
        self.curve.setData([], [])
        self.latest_label.setText("最新值：—")

    def reset_session(self):
        # 停止旧监控
        self.timer.stop()
        self.monitoring = False
        self.btn_toggle.setText("开始监控")

        # 清空数据
        self.records.clear()
        self.next_idx = 1
        self.table.setRowCount(0)
        self.curve.setData([], [])

        # 重置解析器与读指针
        self.parser = StreamParser()
        self.file_pos = 0

        # UI 重置
        self.latest_label.setText("最新值：—")
        self.status_label.setText("状态：停止")
        self.set_led("gray")

    def export_csv(self):
        if not self.records:
            QMessageBox.information(self, "提示", "没有历史数据可导出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "history.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("idx,time,hex32,dec\n")
                for r in self.records:
                    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts))
                    f.write(f"{r.idx},{t_str},{r.hex32},{r.dec}\n")
            QMessageBox.information(self, "完成", "导出成功。")
        except OSError as e:
            QMessageBox.critical(self, "错误", f"导出失败：{e}")

    def set_led(self, color: str):
        # color: "green" / "yellow" / "red" / "gray"
        styles = {
            "green": "background-color: #2ecc71",
            "yellow": "background-color: #f1c40f",
            "red": "background-color: #e74c3c",
            "gray": "background-color: #95a5a6",
        }
        style = styles.get(color, styles["gray"])
        self.led.setStyleSheet(
            f"""
            border-radius: 7px;
            border: 1px solid rgba(0,0,0,0.25);
            {style}
            """
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    icon_path = resource_path("icon.ico")
    app.setWindowIcon(QIcon(icon_path))
    w = MainWindow()
    w.setWindowIcon(QIcon(icon_path))
    w.show()
    sys.exit(app.exec())
