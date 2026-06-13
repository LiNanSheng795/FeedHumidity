import sys
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
import serial
import serial.tools.list_ports

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QTableWidget, QTableWidgetItem, QSpinBox, QMessageBox,
    QAbstractItemView, QLineEdit, QGridLayout, QGroupBox, QComboBox
)

import pyqtgraph as pg
from collections import deque
from statistics import median, mean


def resource_path(relative_path: str) -> str:
    # PyInstaller 单文件运行时会把资源解压到临时目录：sys._MEIPASS
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


# 原始数据报头
HEADER = ["01", "03", "04"]

# AD38 默认读取命令：01 03 00 00 00 02 C4 0B
# 含义：地址1，功能码03，从寄存器0开始读取2个寄存器
READ_CMD = bytes.fromhex("01 03 00 00 00 02 C4 0B")

SERIAL_INTERVAL_MS = 100  # 串口采集时间间隔
FILTER_INTERVAL_MS = 3000  # 屏幕显示刷新时间间隔


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

            # HH hh LL ll -> 32bit
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

        self.serial_port = None
        self.read_cmd = READ_CMD

        #------------ 数据缓存、滤波窗口大小 --------------#
        self.raw_buffer = deque(maxlen=100)  # 原始AD缓存
        self.median_buffer = deque(maxlen=5)  # 中值滤波后的缓存

        self.median_window = 21  # 中值窗口
        self.average_window = 5  # 滑动平均窗口

        self.latest_hex32 = None
        self.latest_raw_dec = None
        #---------------------------------------------#

        self.records: List[ParsedRecord] = []
        self.next_idx = 1

        self.k: float = 0.0
        self.is_calibrated: bool = False

        # --- UI ---
        root = QVBoxLayout(self)

        # Top bar
        top = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(100)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["2400", "4800", "9600", "19200", "28800", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText("9600")
        self.baud_combo.setMinimumWidth(90)

        self.btn_refresh_ports = QPushButton("刷新串口")
        self.btn_refresh_ports.clicked.connect(self.refresh_ports)

        self.btn_serial = QPushButton("打开串口")
        self.btn_serial.clicked.connect(self.toggle_serial)

        self.led = QLabel()
        self.led.setFixedSize(14, 14)  # 圆点大小
        self.led.setToolTip("状态指示灯")
        self.set_led("gray")
        self.status_label = QLabel("状态：停止")
        self.latest_label = QLabel("当前含水量：—")
        self.latest_label.setStyleSheet("font-size: 22px; font-weight: 700; color: blue")

        self.btn_toggle = QPushButton("开始采集")
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.clicked.connect(self.toggle_monitor)

        top.addWidget(QLabel("串口："))
        top.addWidget(self.port_combo)
        top.addWidget(QLabel("波特率："))
        top.addWidget(self.baud_combo)
        top.addWidget(self.btn_refresh_ports)
        top.addWidget(self.btn_serial)
        top.addWidget(self.led)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.latest_label, 2)
        top.addWidget(self.btn_toggle)
        root.addLayout(top)

        self.refresh_ports()

        # Plot
        pg.setConfigOption('background', '#EAEFEF')
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True)

        # bottom: idx（历史）
        self.plot.setLabel("bottom", "序号 idx")
        self.plot.setLabel("left", "含水量 y (%)")
        self.plot.showAxis('top')
        self.plot.getAxis('top').setLabel("AD值")

        # ① 历史曲线：直接画在主 PlotWidget（主 ViewBox）
        self.curve_history = self.plot.plot([], [], pen=pg.mkPen('#3498db', width=2), name="历史曲线")

        # ② 拟合曲线：单独 ViewBox，挂到 top 轴
        self.vb_fit = pg.ViewBox()
        self.plot.scene().addItem(self.vb_fit)

        self.plot.getAxis('top').linkToView(self.vb_fit)  # top 轴跟随 vb_fit
        self.vb_fit.setYLink(self.plot.getViewBox())  # y 轴跟主图一致（关键！）

        self.curve_fit = pg.PlotCurveItem(pen=pg.mkPen('#e74c3c', width=2), name="拟合线")
        self.vb_fit.addItem(self.curve_fit)

        # 视图联动
        self.plot.getPlotItem().vb.sigResized.connect(self.update_views)

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

        # --------------Timer 定时器 -----------------#
        # 串口采集定时器
        self.serial_timer = QTimer(self)
        self.serial_timer.setInterval(SERIAL_INTERVAL_MS)
        self.serial_timer.timeout.connect(self.poll_serial)

        # 滤波显示定时器
        self.filter_timer = QTimer(self)
        self.filter_timer.setInterval(FILTER_INTERVAL_MS)
        self.filter_timer.timeout.connect(self.update_filtered_display)

        self.monitoring = False

    def refresh_ports(self):
        """刷新当前电脑可用的串口列表。"""
        self.port_combo.clear()
        if serial is None:
            self.status_label.setText("状态：未安装 pyserial")
            self.set_led("red")
            return

        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            # p.device: COM3；p.description: USB-SERIAL CH340 等
            self.port_combo.addItem(p.device, p.description)

        if ports:
            self.status_label.setText("状态：请选择串口")
            self.set_led("yellow")
        else:
            self.status_label.setText("状态：未发现串口")
            self.set_led("gray")

    def toggle_serial(self):
        """打开/关闭串口。"""
        if self.serial_port and self.serial_port.is_open:
            self.close_serial()
            return

        if serial is None:
            QMessageBox.critical(self, "缺少依赖", "未安装 pyserial，请先执行：pip install pyserial")
            self.status_label.setText("状态：未安装 pyserial")
            self.set_led("red")
            return

        port = self.port_combo.currentText().strip()
        if not port:
            QMessageBox.warning(self, "提示", "没有可用串口，请检查 USB转485 是否已连接。")
            return

        baud = int(self.baud_combo.currentText())

        try:
            self.serial_port = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=0.25,
                write_timeout=0.25
            )
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()
        except Exception as e:
            self.serial_port = None
            QMessageBox.critical(self, "串口打开失败", f"无法打开 {port}：\n{e}")
            self.status_label.setText("状态：串口打开失败")
            self.set_led("red")
            return

        self.reset_session(clear_serial=False)
        self.btn_serial.setText("关闭串口")
        self.btn_toggle.setEnabled(True)
        self.port_combo.setEnabled(False)
        self.baud_combo.setEnabled(False)
        self.btn_refresh_ports.setEnabled(False)
        self.status_label.setText(f"状态：串口已打开 {port}@{baud}")
        self.set_led("yellow")

    def close_serial(self):
        """关闭串口，并停止采集。"""
        self.serial_timer.stop()
        self.filter_timer.stop()
        self.monitoring = False
        self.btn_toggle.setText("开始采集")
        self.btn_toggle.setEnabled(False)

        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()
        except Exception:
            pass
        self.serial_port = None

        self.btn_serial.setText("打开串口")
        self.port_combo.setEnabled(True)
        self.baud_combo.setEnabled(True)
        self.btn_refresh_ports.setEnabled(True)
        self.status_label.setText("状态：串口已关闭")
        self.set_led("gray")

    def toggle_monitor(self):
        if not (self.serial_port and self.serial_port.is_open):
            QMessageBox.warning(self, "提示", "请先打开串口。")
            return

        self.monitoring = not self.monitoring
        if self.monitoring:
            self.serial_timer.start()
            self.filter_timer.start()
            self.btn_toggle.setText("暂停采集")
            self.status_label.setText("状态：采集中")
            self.set_led("green")
        else:
            self.serial_timer.stop()
            self.filter_timer.stop()
            self.btn_toggle.setText("开始采集")
            self.status_label.setText("状态：暂停")
            self.set_led("yellow")

    def poll_serial(self):
        """定时发送 MODBUS 读取命令，接收 9 字节返回帧并解析。"""
        if not (self.serial_port and self.serial_port.is_open):
            self.serial_timer.stop()
            self.filter_timer.stop()
            self.monitoring = False
            self.btn_toggle.setText("开始采集")
            self.status_label.setText("状态：串口未打开")
            self.set_led("red")
            return

        try:
            self.serial_port.reset_input_buffer()
            self.serial_port.write(self.read_cmd)
            data = self.serial_port.read(9)
        except Exception as e:
            self.serial_timer.stop()
            self.filter_timer.stop()
            self.monitoring = False
            self.btn_toggle.setText("开始采集")
            self.status_label.setText("状态：串口通信失败")
            self.set_led("red")
            QMessageBox.critical(self, "串口通信失败", str(e))
            return

        if len(data) < 9:
            self.status_label.setText(f"状态：等待数据/返回不足({len(data)}/9)")
            self.set_led("yellow")
            return

        parsed = self.parse_modbus_response(data)
        if parsed is None:
            self.status_label.setText("状态：返回帧格式或CRC错误")
            self.set_led("red")
            return

        hex32, dec = parsed
        self.add_record(hex32, dec)
        self.status_label.setText("状态：采集中")
        self.set_led("green")

    @staticmethod
    def modbus_crc16(data: bytes) -> int:
        """MODBUS-RTU CRC16，返回值低字节在前。"""
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc & 0xFFFF

    def parse_modbus_response(self, data: bytes) -> Optional[Tuple[str, int]]:
        """
        解析 AD38 返回帧：01 03 04 LL ll HH hh CRC_L CRC_H
        有效值按 HH hh LL ll 拼成 32 位；协议说明为带符号数据。
        """
        if len(data) < 9:
            return None
        frame = data[:9]

        if frame[0] != 0x01 or frame[1] != 0x03 or frame[2] != 0x04:
            return None

        crc_calc = self.modbus_crc16(frame[:7])
        crc_recv = frame[7] | (frame[8] << 8)
        if crc_calc != crc_recv:
            return None

        ll = frame[3]
        l = frame[4]
        hh = frame[5]
        h = frame[6]

        raw = (hh << 24) | (h << 16) | (ll << 8) | l
        # 协议写明数据类型为带符号；正数不受影响，负数按 int32 还原。
        dec = raw - 0x100000000 if raw >= 0x80000000 else raw
        hex32 = f"{raw:08X}"
        return hex32, dec

    def add_record(self, hex32: str, dec: int):

        self.latest_hex32 = hex32
        self.latest_raw_dec = dec

        self.raw_buffer.append(dec)

    def get_filtered_dec(self):

        if len(self.raw_buffer) < self.median_window:
            return None
        # 中值滤波
        recent_raw = list(self.raw_buffer)[-self.median_window:]
        med_value = median(recent_raw)
        self.median_buffer.append(med_value)
        # 滑动平均
        recent_med = list(self.median_buffer)[-self.average_window:]
        filtered_dec = mean(recent_med)

        return int(round(filtered_dec))

    def update_filtered_display(self):

        filtered_dec = self.get_filtered_dec()
        if filtered_dec is None:
            return
        hex32 = f"{filtered_dec:08X}"

        y = filtered_dec * self.k if self.is_calibrated else 0.0

        rec = ParsedRecord(
            idx=self.next_idx,
            ts=time.time(),
            hex32=hex32,
            dec=filtered_dec,
            y=y
        )

        self.records.append(rec)
        self.append_row(rec)
        self.next_idx += 1

        self.latest_label.setText(
            f"当前含水量：{y:.3f}%"
        )

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

    def update_views(self):
        vb_main = self.plot.getPlotItem().vb
        self.vb_fit.setGeometry(vb_main.sceneBoundingRect())
        self.vb_fit.linkedViewChanged(vb_main, self.vb_fit.YAxis)

    def refresh_plot(self):
        n = int(self.spin_n.value())
        data = self.records[-n:] if len(self.records) > n else self.records
        if not data:
            self.curve_history.setData([], [])
            self.curve_fit.setData([], [])
            return

        # 🔵 历史曲线（idx vs y）
        x_hist = [r.idx for r in data]
        y_hist = [r.y for r in data]
        self.curve_history.setData(x_hist, y_hist)

        # 🔴 拟合曲线（AD vs y）
        if self.is_calibrated:
            ad_min = min(r.dec for r in data)
            ad_max = max(r.dec for r in data)
            x_fit = [ad_min, ad_max]
            y_fit = [ad_min * self.k, ad_max * self.k]
            self.curve_fit.setData(x_fit, y_fit)
        else:
            self.curve_fit.setData([], [])

        self.plot.setLabel('bottom', '时间')
        self.plot.getAxis('top').setLabel('AD值')
        self.plot.showAxis('top')

        # 更新刻度
        self._apply_y_ticks()

        idx_min = min(r.idx for r in data)
        idx_max = max(r.idx for r in data)
        self._apply_idx_ticks(idx_min, idx_max)

        if self.is_calibrated:
            ad_min = min(r.dec for r in data)
            ad_max = max(r.dec for r in data)
            self._apply_ad_ticks(ad_min, ad_max)

    def _apply_y_ticks(self):
        # 纵轴主刻度 0.1（显示为 0.0, 0.1, 0.2 ... ）
        step_y = 0.1
        ticks_y = [(v, f"{v:.1f}") for v in [i * step_y for i in range(int(30 / step_y) + 1)]]
        self.plot.getAxis("left").setTicks([ticks_y])
        self.plot.enableAutoRange(axis='y', enable=True)  # 每次数据更新绘图时y坐标轴的自动适配
        self.plot.setAutoVisible(y=True)  # 只根据可见数据自动调整

    def _apply_idx_ticks(self, x_min, x_max):
        step = 50  # 每50个采样点一个刻度，可根据数据量调整
        start = (x_min // step) * step
        ticks = [(v, str(v)) for v in range(start, x_max + step, step)]
        self.plot.getAxis("bottom").setTicks([ticks])

    def _apply_ad_ticks(self, ad_min, ad_max):
        step = 500
        start = (ad_min // step) * step
        ticks = [(v, str(v)) for v in range(start, ad_max + step, step)]
        self.plot.getAxis("top").setTicks([ticks])

    def clear_history(self):
        self.records.clear()
        self.next_idx = 1
        self.table.setRowCount(0)
        self.curve_history.setData([], [])
        self.curve_fit.setData([], [])
        self.latest_label.setText("当前含水量：—")

    def reset_session(self, clear_serial: bool = False):
        # 停止旧监控
        self.serial_timer.stop()
        self.filter_timer.stop()
        self.monitoring = False
        self.btn_toggle.setText("开始采集")

        # 清空数据
        self.records.clear()
        self.next_idx = 1
        self.table.setRowCount(0)
        self.curve_history.setData([], [])
        self.curve_fit.setData([], [])

        # 如有需要，也可同时关闭串口
        if clear_serial:
            try:
                if self.serial_port and self.serial_port.is_open:
                    self.serial_port.close()
            except Exception:
                pass
            self.serial_port = None

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

        self.k = (y2 * 100 - y1 * 100) / (dec2 - dec1)
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

        if self.records:
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
