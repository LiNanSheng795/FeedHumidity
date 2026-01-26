import sys
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QTableWidget, QTableWidgetItem, QSpinBox, QMessageBox,
    QAbstractItemView, QLineEdit, QGridLayout, QGroupBox
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
    y: float = 0.0


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
            if self._tokens[i:i + 3] != HEADER:
                i += 1
                continue

            # token 顺序：LL ll HH hh YY YY
            ll = self._tokens[i + 3]
            l = self._tokens[i + 4]
            hh = self._tokens[i + 5]
            h = self._tokens[i + 6]
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
        self.setWindowTitle("力源饲料水分监测仪")  # 窗口名称
        icon_path = resource_path("icon.ico")
        self.setWindowIcon(QIcon(icon_path))
        self.resize(1080, 680)  # 默认窗口大小

        self.file_path: Optional[str] = None
        self.file_pos: int = 0  # 文件指针初始化为0——意思是从头开始读文件
        self.parser = StreamParser()

        self.records: List[ParsedRecord] = []
        self.next_idx = 1

        self.k: float = 0.0
        self.is_calibrated: bool = False

        # --- UI ---
        root = QVBoxLayout(self)

        # Top bar
        top = QHBoxLayout()
        self.path_label = QLabel("文件：未选择")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.led = QLabel()
        self.led.setFixedSize(14, 14)  # 圆点大小
        self.led.setToolTip("状态指示灯")
        self.set_led("gray")
        self.status_label = QLabel("状态：停止")
        self.latest_label = QLabel("当前含水量：—")
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
        self.plot.setLabel("bottom", "AD值")
        self.plot.setLabel("left", "含水量 y (%)")
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

        ctrl.addSpacing(10)

        ctrl.addStretch(1)
        left.addLayout(ctrl)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["序号", "时间", "原始数据(32bit)", "AD值", "含水量y（%）"])
        self.table.setColumnWidth(0, 60)  # 序号 列宽度
        self.table.setColumnWidth(1, 120)  # 时间 列宽度
        self.table.setColumnWidth(2, 110)  # 原始数据(32bit) 列宽度
        self.table.setColumnWidth(3, 110)  # AD值 列宽度
        self.table.setColumnWidth(4, 100)  # 含水量y 列宽度
        self.table.verticalHeader().setVisible(False)  # 隐藏左侧行号
        self.table.horizontalHeader().setStretchLastSection(True)  # 最后一列自动填满剩余空间
        self.table.setSelectionMode(QAbstractItemView.NoSelection)  # 设置表格不可选中
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)  # 设置表格不能编辑
        left.addWidget(self.table, 1)

        # ===== 标定面板（右侧）=====
        panel_widget = QGroupBox("标定参数")
        panel_widget.setStyleSheet("""
        QGroupBox {
            background-color: #f7f7f7;
            border: 1px solid #d0d0d0;
            border-radius: 6px;
            margin-top: 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
            font-weight: 600;
        }
        """)

        calib_layout = QGridLayout(panel_widget)
        calib_layout.setContentsMargins(10, 14, 10, 10)
        calib_layout.setHorizontalSpacing(8)
        calib_layout.setVerticalSpacing(8)

        # 公式（富文本）
        self.formula_label = QLabel()
        self.formula_label.setTextFormat(Qt.RichText)
        self.formula_label.setWordWrap(True)
        self.formula_label.setStyleSheet("""
        QLabel{
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            padding: 10px 12px;
            color: #333;
        }
        """)

        self.formula_label.setText("""
        <div style="font-family:'Times New Roman','SimSun'; font-size:16pt; line-height:1.6;">
          <div style="font-size:18pt; margin-bottom:6px;"><b>公式</b></div>

          <div style="margin-left:2px;">
            <span style="font-style:italic;">k</span> = 
            ( <span style="font-style:italic;">y</span><sub>2</sub> − <span style="font-style:italic;">y</span><sub>1</sub> )
            /
            ( AD<sub>2</sub> − AD<sub>1</sub> )
          </div>

          <div style="margin-top:6px; margin-left:2px;">
            <span style="font-style:italic;">y</span> = AD × <span style="font-style:italic;">k</span>
          </div>
        </div>
        """)

        calib_layout.addWidget(self.formula_label, 0, 0, 1, 4)

        self.dec1_edit = QLineEdit()
        self.y1_edit = QLineEdit()
        self.dec2_edit = QLineEdit()
        self.y2_edit = QLineEdit()

        self.btn_cal = QPushButton("计算标定")
        self.btn_cal.clicked.connect(self.calibrate)

        self.k_label = QLabel("k=—")
        self.k_label.setMinimumWidth(140)

        # 行1：dec1 + y1
        lbl_dec1 = QLabel("AD₁")
        lbl_y1 = QLabel("y₁(%)")
        for lb in (lbl_dec1, lbl_y1):
            lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lb.setStyleSheet("color:#444; font-size:14pt; font-weight:600;")

        self.dec1_edit.setPlaceholderText("低AD值")
        self.y1_edit.setPlaceholderText("低含水量(%)")
        self.dec1_edit.setFixedHeight(30)
        self.y1_edit.setFixedHeight(30)
        self.dec1_edit.setStyleSheet("font-size:12pt;")
        self.y1_edit.setStyleSheet("font-size:12pt;")

        calib_layout.addWidget(lbl_dec1, 1, 0)
        calib_layout.addWidget(self.dec1_edit, 1, 1)
        calib_layout.addWidget(lbl_y1, 1, 2)
        calib_layout.addWidget(self.y1_edit, 1, 3)

        # 行2：dec2 + y2
        lbl_dec2 = QLabel("AD₂")
        lbl_y2 = QLabel("y₂(%)")
        for lb in (lbl_dec2, lbl_y2):
            lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lb.setStyleSheet("color:#444; font-size:14pt; font-weight:600;")

        self.dec2_edit.setPlaceholderText("高AD值")
        self.y2_edit.setPlaceholderText("高含水量(%)")
        self.dec2_edit.setFixedHeight(30)
        self.y2_edit.setFixedHeight(30)
        self.dec2_edit.setStyleSheet("font-size:12pt;")
        self.y2_edit.setStyleSheet("font-size:12pt;")

        calib_layout.addWidget(lbl_dec2, 2, 0)
        calib_layout.addWidget(self.dec2_edit, 2, 1)
        calib_layout.addWidget(lbl_y2, 2, 2)
        calib_layout.addWidget(self.y2_edit, 2, 3)

        # 按钮 + k 显示（同一行更紧凑）
        self.btn_cal.setFixedHeight(32)
        self.k_label.setStyleSheet("""
        QLabel{
            background: white;
            color: blue;
            font-size: 16pt;
            font-weight: bold;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            padding: 0px 0px;
        }
        """)

        calib_layout.addWidget(self.btn_cal, 3, 0, 1, 2)
        calib_layout.addWidget(self.k_label, 3, 2, 1, 2)

        # 让输入框两列更均衡
        calib_layout.setColumnStretch(1, 1)
        calib_layout.setColumnStretch(3, 1)
        calib_layout.setColumnMinimumWidth(0, 46)
        calib_layout.setColumnMinimumWidth(2, 46)

        mid.addLayout(left, 2)
        mid.addWidget(panel_widget, 1)
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
        self.latest_label.setText("当前含水量：—")
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
            y = dec * self.k if self.is_calibrated else 0.0
            rec = ParsedRecord(idx=self.next_idx, ts=now, hex32=hex32, dec=dec, y=y)
            self.records.append(rec)
            self.append_row(rec)
            self.next_idx += 1
            if self.is_calibrated:
                self.latest_label.setText(f"当前含水量：{y:.3f}%")
            else:
                self.latest_label.setText(f"当前含水量：{y:.3f}%")
            if self.monitoring:
                self.set_led("green")

        self.refresh_plot()

    def add_center_item(self, row, col, text):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)
    
    def append_row(self, rec: ParsedRecord):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.add_center_item(row, 0, str(rec.idx))
        t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.ts))
        self.add_center_item(row, 1, t_str)
        self.add_center_item(row, 2, rec.hex32)
        self.add_center_item(row, 3, str(rec.dec))
        self.add_center_item(row, 4, f"{rec.y:.3f}")

        # 自动滚到最新
        self.table.scrollToBottom()

    def refresh_plot(self):
        n = int(self.spin_n.value())
        data = self.records[-n:] if len(self.records) > n else self.records
        if not data:
            self.curve.setData([], [])
            return

        x = [r.dec for r in data]
        y = [r.y for r in data]

        self.curve.setData(x, y)

        # y 轴满量程 0~30
        # self.plot.setYRange(0, 30, padding=0)

        # 设置轴标签
        self.plot.setLabel("bottom", "AD值")
        self.plot.setLabel("left", "含水量 y (%)")

        # 设置刻度（主刻度：x=500, y=0.5）
        self._apply_axis_ticks(x_min=min(x), x_max=max(x))

    def _apply_axis_ticks(self, x_min: int, x_max: int):
        # 横轴主刻度 500
        step_x = 500
        start_x = (x_min // step_x) * step_x
        ticks_x = [(v, str(v)) for v in range(start_x, x_max + step_x, step_x)]
        self.plot.getAxis("bottom").setTicks([ticks_x])

        # 纵轴主刻度 0.1（显示为 0.0, 0.1, 0.2 ... ）
        step_y = 0.1
        ticks_y = [(v, f"{v:.1f}") for v in [i * step_y for i in range(int(30 / step_y) + 1)]]
        self.plot.getAxis("left").setTicks([ticks_y])
        self.plot.enableAutoRange(axis='y', enable=True)  # 每次数据更新绘图时y坐标轴缩放的自动适配

    def clear_history(self):
        self.records.clear()
        self.next_idx = 1
        self.table.setRowCount(0)
        self.curve.setData([], [])
        self.latest_label.setText("当前含水量：—")

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
        self.latest_label.setText("当前含水量：—")
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
            with open(path, "w", encoding="utf-8-sig", newline="") as f:

                f.write("序号,时间,原始数据(32bit),AD值,含水量y（%）\n")
                for r in self.records:
                    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts))
                    f.write(f"{r.idx},{t_str},{r.hex32},{r.dec},{r.y:.3f}\n")
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

    def calibrate(self):
        try:
            dec1 = float(self.dec1_edit.text().strip())
            y1 = float(self.y1_edit.text().strip())
            dec2 = float(self.dec2_edit.text().strip())
            y2 = float(self.y2_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "输入错误", "输入的内容 必须是数字。")
            return

        if dec2 == dec1:
            QMessageBox.warning(self, "输入错误", "高AD值 不能等于 低AD值（否则分母为0无法计算 k）。")
            return

        self.k = (y2 - y1) / (dec2 - dec1)
        self.is_calibrated = True
        self.k_label.setText(f"k={self.k:.6f}")

        # 更新所有历史 y，并刷新表格 y 列
        for r in self.records:
            r.y = r.dec * self.k

        for row in range(self.table.rowCount()):
            dec_item = self.table.item(row, 3)
            if dec_item is None:
                continue
            dec_val = int(dec_item.text())
            y_val = dec_val * self.k
            self.add_center_item(row, 4, f"{y_val:.3f}")

        self.latest_label.setText(f"当前含水量：{self.records[-1].y:.3f}%")

        self.refresh_plot()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    icon_path = resource_path("icon.ico")
    app.setWindowIcon(QIcon(icon_path))
    w = MainWindow()
    w.setWindowIcon(QIcon(icon_path))
    w.show()
    sys.exit(app.exec())
