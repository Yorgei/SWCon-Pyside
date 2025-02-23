import logging
import math
import os
import platform
import queue
import sys
import time
from ctypes import *
from logging import getLogger
from pathlib import Path
from typing import Optional

import cv2
import PySide6
import numpy as np
import shiboken6
from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from libs import sender
from libs.capture import CaptureWorker
from libs.com_port_assist import serial_ports
from libs.CommandBase import CommandBase
from libs.CommandLoader import CommandLoader
from libs.game_pad_connect import GamepadController
from libs.keys import Button, Direction, Hat, KeyPress, Stick
from libs.mcu_command_base import McuCommand
from libs.settings import Setting
from libs.Utility import ospath
from ui.main_ui import Ui_MainWindow
from ui.QtextLogger import QPlainTextEditLogger
from libs.LineNotify import LineNotify

VERSION = "0.7.2 (beta)"
Author = "Moi"


# Todo
# GUIの起動
class MainWindow(QMainWindow, Ui_MainWindow):
    stop_request = Signal(bool)

    def __init__(
            self,
            parent: Optional[PySide6.QtWidgets.QWidget] = None,
            flags: PySide6.QtCore.Qt.WindowFlags = QtCore.Qt.Window,
    ) -> None:
        """_summary_

        Args:
            parent (Optional[PySide6.QtWidgets.QWidget], optional): _description_. Defaults to None.
            flags (PySide6.QtCore.Qt.WindowFlags, optional): _description_. Defaults to QtCore.Qt.Window.
        """
        super().__init__(parent, flags)
        self.logger = getLogger(__name__)
        self.thread_1 = None
        self.capture_worker = None
        self.thread_2 = None
        self.worker = None
        self.GamepadController_worker = None
        self.GamepadController_thread = QThread()
        self.thread_do = None
        self.img = None
        self.command_mode = None
        self.is_show_serial = False
        self.keyPress = None
        self.keymap = None
        self.gui_l_stick = 0
        self.gui_r_stick = 0

        self.bef_left_angle = None
        self.bef_left_r = None
        self.bef_right_angle = None
        self.bef_right_r = None

        self.setting = Setting()

        self.setupUi(self)
        self.setWindowTitle(f"SWController {VERSION}")

        self.pushButtonReloadCamera.pressed.connect(lambda: self.reconnect_camera(self.lineEditCameraID.text()))
        self.pushButtonReloadCamera.pressed.connect(self.reload_camera)

        self.plainTextEdit = QPlainTextEditLogger(self.dockWidgetContents)
        self.plainTextEdit.widget.setObjectName("plainTextEdit")
        self.plainTextEdit.widget.setEnabled(True)
        self.plainTextEdit.widget.setMinimumSize(QSize(500, 195))
        self.plainTextEdit.widget.setUndoRedoEnabled(True)
        self.plainTextEdit.widget.setCursorWidth(-2)
        self.plainTextEdit.widget.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.gridLayout.addWidget(self.plainTextEdit.widget, 0, 0, 1, 1)
        self.setFocus()

        self.set_settings()
        py_cmd = self.setting.setting["command"]["py_command"]
        mcu_cmd = self.setting.setting["command"]["mcu_command"]

        # self.gamepad_setting = SettingWindow(parent=self, _setting=self.setting)
        # self.gamepad_setting.show()

        self.q: queue.Queue = queue.Queue()
        self.setup_functions_connect()

        try:
            # camera名取得はうまくいかない
            if platform.system() == 'Windows':
                import clr
                clr.AddReference("./DirectShowLib/DirectShowLib-2005")
                from DirectShowLib import DsDevice, FilterCategory

                # print(DirectShowLib)
                capture_devices = DsDevice.GetDevicesOfCat(FilterCategory.VideoInputDevice)
                self.camera_dic = {cam_id: device.Name for cam_id, device in enumerate(capture_devices)}
                self.camera_dic[str(max(list(self.camera_dic.keys())) + 1)] = ''
                print([device for device in self.camera_dic.values()])
            else:
                self.comboBoxCameraNames.setDisabled(True)
        except Exception as e:
            self.comboBoxCameraNames.setDisabled(True)
            pass

        self.connect_capture()

        # You can format what is printed to text box
        self.plainTextEdit.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        # You can control the logging level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.plainTextEdit)

        self.pushButtonClearLog.pressed.connect(self.plainTextEdit.widget.clear)

        self.ser = sender.Sender(self.is_show_serial)
        self.ser.print_strings.connect(self.callback_string_to_log)
        self.activate_serial()
        self.activate_keyboard()
        self.connect_gamepad()
        self.load_commands()
        self.show_serial()

        try:
            self.comboBox_MCU.setCurrentIndex(self.comboBoxPython.findText(mcu_cmd))
            self.comboBoxPython.setCurrentIndex(self.comboBoxPython.findText(py_cmd))
        except Exception as e:
            self.logger.debug("There seems to have been a change in the script.")

        # スレッドの開始
        if self.thread_1 is not None:
            self.thread_1.start()
        self.GamepadController_thread.start()

        if self.setting is not None:
            if self.setting.setting["main_window"]["option"]["window_showMaximized"]:
                self.showMaximized()
            else:
                self.resize(
                    self.setting.setting["main_window"]["option"]["window_size_width"],
                    self.setting.setting["main_window"]["option"]["window_size_height"],
                )
        # self.pushButtonScreenShot.clicked.connect(self.test)
        # self.CaptureImageArea.mousePressEvent = self.capture_mousePressEvent
        # print(self.setting.setting["line"])
        self.line_notify = LineNotify(tokens=self.setting.setting["line"])
        self.line_notify.print_strings.connect(self.callback_string_to_log)
        # self.logger.debug(self.line_notify.send_text("test", token_key="token_3"))

    def test(self):
        self.BTN_a.toggle()

    def gui_stick_left(self, angle, r):
        self.gui_l_stick = r
        self.keyPress.input(Direction(Stick.LEFT, angle, r))

    def gui_stick_right(self, angle, r):
        self.gui_r_stick = r
        self.keyPress.input(Direction(Stick.RIGHT, angle, r))

    # <editor-fold desc="ゲームパッド関連">
    def connect_gamepad(self):
        # Controllerスレッドの作成
        try:
            self.GamepadController_worker = GamepadController()
            self.GamepadController_thread = QThread()
            self.GamepadController_worker.moveToThread(self.GamepadController_thread)
            self.GamepadController_thread.started.connect(self.GamepadController_worker.run)

            self.GamepadController_worker.set_keymap(self.keymap)

            self.GamepadController_worker.ZL_PRESSED.connect(self.press_zl)
            self.GamepadController_worker.L_PRESSED.connect(self.press_l)
            self.GamepadController_worker.LCLICK_PRESSED.connect(self.press_lclick)
            self.GamepadController_worker.MINUS_PRESSED.connect(self.press_minus)
            self.GamepadController_worker.TOP_PRESSED.connect(self.press_top)
            self.GamepadController_worker.BTM_PRESSED.connect(self.press_btm)
            self.GamepadController_worker.LEFT_PRESSED.connect(self.press_left)
            self.GamepadController_worker.RIGHT_PRESSED.connect(self.press_right)
            self.GamepadController_worker.CAPTURE_PRESSED.connect(self.press_capture)
            self.GamepadController_worker.ZR_PRESSED.connect(self.press_zr)
            self.GamepadController_worker.R_PRESSED.connect(self.press_r)
            self.GamepadController_worker.RCLICK_PRESSED.connect(self.press_rclick)
            self.GamepadController_worker.PLUS_PRESSED.connect(self.press_plus)
            self.GamepadController_worker.A_PRESSED.connect(self.press_a)
            self.GamepadController_worker.B_PRESSED.connect(self.press_b)
            self.GamepadController_worker.X_PRESSED.connect(self.press_x)
            self.GamepadController_worker.Y_PRESSED.connect(self.press_y)
            self.GamepadController_worker.HOME_PRESSED.connect(self.press_home)

            self.GamepadController_worker.ZL_RELEASED.connect(self.release_zl)
            self.GamepadController_worker.L_RELEASED.connect(self.release_l)
            self.GamepadController_worker.LCLICK_RELEASED.connect(self.release_lclick)
            self.GamepadController_worker.MINUS_RELEASED.connect(self.release_minus)
            self.GamepadController_worker.TOP_RELEASED.connect(self.release_top)
            self.GamepadController_worker.BTM_RELEASED.connect(self.release_btm)
            self.GamepadController_worker.LEFT_RELEASED.connect(self.release_left)
            self.GamepadController_worker.RIGHT_RELEASED.connect(self.release_right)
            self.GamepadController_worker.CAPTURE_RELEASED.connect(self.release_capture)
            self.GamepadController_worker.ZR_RELEASED.connect(self.release_zr)
            self.GamepadController_worker.R_RELEASED.connect(self.release_r)
            self.GamepadController_worker.RCLICK_RELEASED.connect(self.release_rclick)
            self.GamepadController_worker.PLUS_RELEASED.connect(self.release_plus)
            self.GamepadController_worker.A_RELEASED.connect(self.release_a)
            self.GamepadController_worker.B_RELEASED.connect(self.release_b)
            self.GamepadController_worker.X_RELEASED.connect(self.release_x)
            self.GamepadController_worker.Y_RELEASED.connect(self.release_y)
            self.GamepadController_worker.HOME_RELEASED.connect(self.release_home)

            self.GamepadController_worker.AXIS_MOVED.connect(self.stickMoveEvent, type=Qt.QueuedConnection)
            self.GamepadController_worker.AXIS_MOVED.connect(self.stick_control, type=Qt.DirectConnection)

            self.GamepadController_worker.print_strings.connect(self.callback_string_to_log, type=Qt.QueuedConnection)

            self.gamepad_l_stick()
            self.gamepad_r_stick()

            self.GamepadController_thread.finished.connect(self.GamepadController_worker.deleteLater)
            self.GamepadController_thread.finished.connect(self.GamepadController_worker.stop)

            if self.GamepadController_worker.no_joystick:
                self.logger.debug("コントローラー接続なし")
        except Exception as e:
            self.logger.error(e)

    def stickMoveEvent(self, left_horizontal, left_vertical, right_horizontal, right_vertical):
        dead_zone = 0.05  # これ以下の傾きは無視(デッドゾーン)
        left_angle = math.atan2(left_vertical, left_horizontal)
        left_r = math.sqrt(left_vertical ** 2 + left_horizontal ** 2)
        right_angle = math.atan2(right_vertical, right_horizontal)
        right_r = math.sqrt(right_vertical ** 2 + right_horizontal ** 2)

        # print(left_r, right_r)
        if left_r < dead_zone:
            left_r = 0
        if right_r < dead_zone:
            right_r = 0

        self.left_stick.stickMoveEvent(left_r, left_angle)
        self.right_stick.stickMoveEvent(right_r, right_angle)

    def reconnect_gamepad(self):
        self.GamepadController_worker.connect_joystick()
        pass

    def BTN_click(self, event=None):
        btn = self.sender().objectName()[4:]
        match btn:
            case "zl":
                if self.sender().isChecked():
                    self.press_zl()
                else:
                    self.release_zl()
            case "l":
                if self.sender().isChecked():
                    self.press_l()
                else:
                    self.release_l()
            case "up":
                if self.sender().isChecked():
                    self.press_top()
                else:
                    self.release_top()
            case "down":
                if self.sender().isChecked():
                    self.press_btm()
                else:
                    self.release_btm()
            case "left":
                if self.sender().isChecked():
                    self.press_left()
                else:
                    self.release_left()
            case "right":
                if self.sender().isChecked():
                    self.press_right()
                else:
                    self.release_right()
            case "capture":
                if self.sender().isChecked():
                    self.press_capture()
                else:
                    self.release_capture()
            case "ls":
                if self.sender().isChecked():
                    self.press_lclick()
                else:
                    self.release_lclick()
            case "minus":
                if self.sender().isChecked():
                    self.press_minus()
                else:
                    self.release_minus()
            case "zr":
                if self.sender().isChecked():
                    self.press_zr()
                else:
                    self.release_zr()
            case "r":
                if self.sender().isChecked():
                    self.press_r()
                else:
                    self.release_r()
            case "plus":
                if self.sender().isChecked():
                    self.press_plus()
                else:
                    self.release_plus()
            case "rs":
                if self.sender().isChecked():
                    self.press_rclick()
                else:
                    self.release_rclick()
            case "a":
                if self.sender().isChecked():
                    self.press_a()
                else:
                    self.release_a()
            case "b":
                if self.sender().isChecked():
                    self.press_b()
                else:
                    self.release_b()
            case "x":
                if self.sender().isChecked():
                    self.press_x()
                else:
                    self.release_x()
            case "y":
                if self.sender().isChecked():
                    self.press_y()
                else:
                    self.release_y()
            case "home":
                if self.sender().isChecked():
                    self.press_home()
                else:
                    self.release_home()

    def press_zl(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_zl.setChecked(True)

        self.keyPress.input(Button.ZL)

    def press_l(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_l.setChecked(True)
        self.keyPress.input(Button.L)

    def press_lclick(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_ls.setChecked(True)
        self.keyPress.input(Button.LCLICK)

    def press_minus(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_minus.setChecked(True)
        self.keyPress.input(Button.MINUS)

    def press_top(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_up.setChecked(True)
        self.keyPress.input(Hat.TOP)

    def press_btm(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_down.setChecked(True)
        self.keyPress.input(Hat.BTM)

    def press_left(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_left.setChecked(True)
        self.keyPress.input(Hat.LEFT)

    def press_right(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_right.setChecked(True)
        self.keyPress.input(Hat.RIGHT)

    def press_capture(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_capture.setChecked(True)
        self.keyPress.input(Button.CAPTURE)

    def press_zr(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_zr.setChecked(True)
        self.keyPress.input(Button.ZR)

    def press_r(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_r.setChecked(True)
        self.keyPress.input(Button.R)

    def press_rclick(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_rs.setChecked(True)
        self.keyPress.input(Button.RCLICK)

    def press_plus(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_plus.setChecked(True)
        self.keyPress.input(Button.PLUS)

    def press_a(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_a.setChecked(True)
        self.keyPress.input(Button.A)

    def press_b(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_b.setChecked(True)
        self.keyPress.input(Button.B)

    def press_x(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_x.setChecked(True)
        self.keyPress.input(Button.X)

    def press_y(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_y.setChecked(True)
        self.keyPress.input(Button.Y)

    def press_home(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_home.setChecked(True)
        self.keyPress.input(Button.HOME)

    def release_zl(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_zl.setChecked(False)
        self.keyPress.inputEnd(Button.ZL)

    def release_l(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_l.setChecked(False)
        self.keyPress.inputEnd(Button.L)

    def release_lclick(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_ls.setChecked(False)
        self.keyPress.inputEnd(Button.LCLICK)

    def release_minus(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_minus.setChecked(False)
        self.keyPress.inputEnd(Button.MINUS)

    def release_top(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_up.setChecked(False)
        self.keyPress.inputEnd(Hat.TOP)

    def release_btm(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_down.setChecked(False)
        self.keyPress.inputEnd(Hat.BTM)

    def release_left(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_left.setChecked(False)
        self.keyPress.inputEnd(Hat.LEFT)

    def release_right(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_right.setChecked(False)
        self.keyPress.inputEnd(Hat.RIGHT)

    def release_capture(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_capture.setChecked(False)
        self.keyPress.inputEnd(Button.CAPTURE)

    def release_zr(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_zr.setChecked(False)
        self.keyPress.inputEnd(Button.ZR)

    def release_r(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_r.setChecked(False)
        self.keyPress.inputEnd(Button.R)

    def release_rclick(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_rs.setChecked(False)
        self.keyPress.inputEnd(Button.RCLICK)

    def release_plus(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_plus.setChecked(False)
        self.keyPress.inputEnd(Button.PLUS)

    def release_a(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_a.setChecked(False)
        self.keyPress.inputEnd(Button.A)

    def release_b(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_b.setChecked(False)
        self.keyPress.inputEnd(Button.B)

    def release_x(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_x.setChecked(False)
        self.keyPress.inputEnd(Button.X)

    def release_y(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_y.setChecked(False)
        self.keyPress.inputEnd(Button.Y)

    def release_home(self):
        try:
            if isinstance(self.sender(), type(self.GamepadController_worker)):
                joystick = True
            elif isinstance(self.sender(), QtWidgets.QPushButton):
                joystick = True
            else:
                joystick = False
        except Exception:
            joystick = False
        if joystick:
            self.BTN_home.setChecked(False)
        self.keyPress.inputEnd(Button.HOME)

    def stick_control(self, left_horizontal, left_vertical, right_horizontal, right_vertical):
        dead_zone = 0.05  # これ以下の傾きは無視(デッドゾーン)
        left_angle = -math.degrees(math.atan2(left_vertical, left_horizontal))
        left_r = math.sqrt(left_vertical ** 2 + left_horizontal ** 2)
        right_angle = -math.degrees(math.atan2(right_vertical, right_horizontal))
        right_r = math.sqrt(right_vertical ** 2 + right_horizontal ** 2)

        # print(left_r, right_r)
        if left_r < dead_zone:
            left_r = 0
        if right_r < dead_zone:
            right_r = 0

        # print(left_angle, left_r)
        # if self.bef_left_r is None or (left_r != 0 and self.bef_left_r != left_r) or (
        #         right_r != 0 and self.bef_right_r != right_r):
        #     self.logger.debug(f"left:{left_angle:.2f}, {left_r:.2f}  right:{right_angle:.2f}, {right_r:.2f}")
        if self.keyPress is not None:
            if self.gui_l_stick == 0 and self.gui_r_stick == 0:
                self.keyPress.input(
                    [Direction(Stick.LEFT, left_angle, left_r), Direction(Stick.RIGHT, right_angle, right_r)]
                )
            elif self.gui_l_stick > 0 and self.gui_r_stick == 0:
                self.keyPress.input([Direction(Stick.RIGHT, right_angle, right_r)])
            elif self.gui_l_stick == 0 and self.gui_r_stick > 0:
                self.keyPress.input([Direction(Stick.LEFT, left_angle, left_r)])

        self.bef_left_angle = left_angle
        self.bef_left_r = left_r
        self.bef_right_angle = right_angle
        self.bef_right_r = right_r

    def gamepad_l_stick(self):
        if self.setting.setting["key_config"]["joystick"]["direction"]["LStick"]:
            self.GamepadController_worker.set_l_stick(True)

    def gamepad_r_stick(self):
        if self.setting.setting["key_config"]["joystick"]["direction"]["RStick"]:
            self.GamepadController_worker.set_r_stick(True)

    # </editor-fold>

    def connect_capture(self):
        self.thread_1 = QThread()
        try:
            self.capture_worker = CaptureWorker(camera_id=int(self.lineEditCameraID.text()))
        except:
            self.capture_worker = CaptureWorker(camera_id=-1)

        self.capture_worker.moveToThread(self.thread_1)

        self.capture_worker.change_pixmap_signal.connect(self.update_image, type=Qt.QueuedConnection)
        self.capture_worker.print_strings.connect(self.callback_string_to_log, type=Qt.QueuedConnection)

        self.thread_1.started.connect(self.capture_worker.run)

        self.thread_1.finished.connect(self.capture_worker.deleteLater)
        self.thread_1.finished.connect(self.thread_1.deleteLater)

    def setup_functions_connect(self):

        # controllerボタンの割当
        self.BTN_zl.pressed.connect(self.press_zl)
        self.BTN_l.pressed.connect(self.press_l)
        self.BTN_ls.pressed.connect(self.press_lclick)
        self.BTN_minus.pressed.connect(self.press_minus)
        self.BTN_up.pressed.connect(self.press_top)
        self.BTN_down.pressed.connect(self.press_btm)
        self.BTN_left.pressed.connect(self.press_left)
        self.BTN_right.pressed.connect(self.press_right)
        self.BTN_capture.pressed.connect(self.press_capture)
        self.BTN_zr.pressed.connect(self.press_zr)
        self.BTN_r.pressed.connect(self.press_r)
        self.BTN_rs.pressed.connect(self.press_rclick)
        self.BTN_plus.pressed.connect(self.press_plus)
        self.BTN_a.pressed.connect(self.press_a)
        self.BTN_b.pressed.connect(self.press_b)
        self.BTN_x.pressed.connect(self.press_x)
        self.BTN_y.pressed.connect(self.press_y)
        self.BTN_home.pressed.connect(self.press_home)

        self.BTN_zl.released.connect(self.release_zl)
        self.BTN_l.released.connect(self.release_l)
        self.BTN_ls.released.connect(self.release_lclick)
        self.BTN_minus.released.connect(self.release_minus)
        self.BTN_up.released.connect(self.release_top)
        self.BTN_down.released.connect(self.release_btm)
        self.BTN_left.released.connect(self.release_left)
        self.BTN_right.released.connect(self.release_right)
        self.BTN_capture.released.connect(self.release_capture)
        self.BTN_zr.released.connect(self.release_zr)
        self.BTN_r.released.connect(self.release_r)
        self.BTN_rs.released.connect(self.release_rclick)
        self.BTN_plus.released.connect(self.release_plus)
        self.BTN_a.released.connect(self.release_a)
        self.BTN_b.released.connect(self.release_b)
        self.BTN_x.released.connect(self.release_x)
        self.BTN_y.released.connect(self.release_y)
        self.BTN_home.released.connect(self.release_home)

        # self.BTN_zl.released.connect(self.release_zl)
        # self.BTN_l.released.connect(self.release_l)
        # self.BTN_up.released.connect(self.release_lclick)
        # self.BTN_down.released.connect(self.release_minus)
        # self.BTN_left.released.connect(self.release_top)
        # self.BTN_right.released.connect(self.release_btm)
        # self.BTN_capture.released.connect(self.release_left)
        # self.BTN_ls.released.connect(self.release_right)
        # self.BTN_minus.released.connect(self.release_capture)
        # self.BTN_zr.released.connect(self.release_zr)
        # self.BTN_r.released.connect(self.release_r)
        # self.BTN_plus.released.connect(self.release_rclick)
        # self.BTN_rs.released.connect(self.release_plus)
        # self.BTN_a.released.connect(self.release_a)
        # self.BTN_b.released.connect(self.release_b)
        # self.BTN_x.released.connect(self.release_x)
        # self.BTN_y.released.connect(self.release_y)
        # self.BTN_home.released.connect(self.release_home)

        # 各ボタンの関数割当
        self.pushButton_PythonStart.pressed.connect(self.start_command)
        self.pushButton_PythonStop.pressed.connect(self.stop_command)

        self.pushButton_MCUStart.pressed.connect(self.start_mcu_command)
        self.pushButton_MCUStop.pressed.connect(self.stop_mcu_command)

        self.pushButton_PythonReload.clicked.connect(self.reload_commands)
        self.pushButton_MCUReload.clicked.connect(self.reload_commands)

        self.pushButtonReloadPort.clicked.connect(self.activate_serial)
        self.pushButtonScreenShot.clicked.connect(self.screen_shot)
        self.toolButtonOpenScreenShotDir.clicked.connect(self.open_screen_shot_dir)
        self.toolButtonOpenPythonDir.clicked.connect(self.open_python_shot_dir)
        self.toolButton_OpenMCUDir.clicked.connect(self.open_mcu_shot_dir)
        self.tabWidget.currentChanged.connect(self.set_command_mode)
        # GUIスティックの割当　(fpsが落ちるのでコントローラー推奨)
        self.left_stick.stick_signal.connect(self.gui_stick_left, type=Qt.DirectConnection)
        self.right_stick.stick_signal.connect(self.gui_stick_right, type=Qt.DirectConnection)

        # 　各コンボボックス選択時の挙動
        self.tabWidget.currentChanged.connect(self.assign_command)
        self.comboBoxPython.currentIndexChanged.connect(self.assign_command)
        self.comboBox_MCU.currentIndexChanged.connect(self.assign_command)

        # キャプチャ画像クリック時に座標を返すように
        self.CaptureImageArea.mousePressEvent = self.capture_mousePressEvent

        # Setting.tomlへの保存
        self.lineEditFPS.textChanged.connect(self.assign_fps_to_setting)
        self.lineEditCameraID.textChanged.connect(self.assign_camera_id_to_setting)
        self.lineEditComPort.textChanged.connect(self.assign_com_port_to_setting)
        self.comboBox_MCU.currentIndexChanged.connect(self.assign_mcu_command_to_setting)
        self.comboBoxPython.currentIndexChanged.connect(self.assign_py_command_to_setting)

        self.actionconnect.triggered.connect(self.reconnect_gamepad)
        self.actionCOM_Port_ASSIST.triggered.connect(self.message_show_available_com_port)

    def assign_fps_to_setting(self):
        self.setting.setting["main_window"]["must"]["fps"] = self.lineEditFPS.text()

    def assign_camera_id_to_setting(self):
        self.setting.setting["main_window"]["must"]["camera_id"] = self.lineEditCameraID.text()

    def assign_com_port_to_setting(self):
        self.setting.setting["main_window"]["must"]["com_port"] = self.lineEditComPort.text()

    def assign_window_size_to_setting(self):
        self.setting.setting["main_window"]["must"]["com_port"] = self.lineEditComPort.text()

    def assign_mcu_command_to_setting(self):
        self.setting.setting["command"]["mcu_command"] = self.comboBox_MCU.itemText(self.comboBox_MCU.currentIndex())

    def assign_py_command_to_setting(self):
        self.setting.setting["command"]["py_command"] = self.comboBoxPython.itemText(self.comboBoxPython.currentIndex())

    def resizeEvent(self, event: PySide6.QtGui.QResizeEvent) -> None:
        self.setting.setting["main_window"]["option"]["window_size_width"] = self.width()
        self.setting.setting["main_window"]["option"]["window_size_height"] = self.height()

    def set_command_mode(self):
        if self.tabWidget.currentIndex() == 0:
            self.command_mode = "python"
        elif self.tabWidget.currentIndex() == 1:
            self.command_mode = "mcu"
        else:
            raise Exception

    @Slot(QImage)
    def update_image(self, image):
        self.img = image
        pix = QPixmap.fromImage(self.img)
        # pix.scaled(1280, 720, aspectMode=QtCore.Qt.KeepAspectRatio)
        self.CaptureImageArea.setPixmap(pix)

    @Slot(str)
    def update_log(self, s):
        # self.plainTextEdit.insertPlainText(s + "\n")
        pass

    @Slot(bool)
    def img_recognition_return(self):
        return self.img

    @Slot(str)
    def callback_run_macro(self, s):
        self.ser.writeRow(s)

    def reconnect_camera(self, cam_id):
        self.capture_worker.open_camera(int(cam_id))

    def activate_serial(self):
        try:
            if self.ser.isOpened():
                print("Port is already opened and being closed.")
                self.ser.closeSerial()
                self.keyPress = None
                self.activate_serial()
            else:
                if self.ser.openSerial(self.setting.setting["main_window"]["must"]["com_port"], ""):
                    self.logger.debug(
                        "COM Port "
                        + str(self.setting.setting["main_window"]["must"]["com_port"])
                        + " connected successfully"
                    )
                    self.keyPress = KeyPress(self.ser)
        except:
            self.logger.debug("Input Correct COM Port and Reload!")
        pass

    # Todo キーボード操作できるようにする
    def activate_keyboard(self):
        pass

    def load_commands(self):
        self.py_loader = CommandLoader(ospath("Commands/Python"), CommandBase)  # コマンドの読み込み
        self.mcu_loader = CommandLoader(ospath("Commands/MCU"), McuCommand)
        self.py_classes = self.py_loader.load()
        self.mcu_classes = self.mcu_loader.load()
        # print(self.mcu_classes)
        self.set_command_items()
        self.assign_command()

    def set_command_items(self):
        i = 0
        for v in [c.NAME for c in self.py_classes]:
            self.comboBoxPython.addItem(v)
            if (s := self.py_classes[i]().__tool_tip__) is not None:
                self.comboBoxPython.setItemData(i, s, QtCore.Qt.ToolTipRole)
            i += 1
        s = None
        self.comboBoxPython.setCurrentIndex(0)
        for v in [c.NAME for c in self.mcu_classes]:
            self.comboBox_MCU.addItem(v)
        self.comboBox_MCU.setCurrentIndex(0)

    def assign_command(self):
        # 選択されているコマンドを取得する
        self.mcu_cur_command = self.mcu_classes[self.comboBox_MCU.currentIndex()]  # MCUコマンドについて

        self.py_cur_command = self.py_classes[self.comboBoxPython.currentIndex()]

        if self.tabWidget.currentIndex() == 0:
            self.cur_command = self.py_cur_command
        else:
            self.cur_command = self.mcu_cur_command

    def reload_commands(self):
        # 表示しているタブを読み取って、どのコマンドを表示しているか取得、リロード後もそれが選択されるようにする
        oldval_mcu = self.comboBox_MCU.itemText(self.comboBox_MCU.currentIndex())
        oldval_py = self.comboBoxPython.itemText(self.comboBoxPython.currentIndex())
        print(oldval_mcu)

        self.comboBox_MCU.clear()
        self.comboBoxPython.clear()

        self.py_classes = self.py_loader.reload()
        self.mcu_classes = self.mcu_loader.reload()

        # Restore the command selecting state if possible
        self.set_command_items()
        if self.comboBox_MCU.findText(oldval_mcu) != -1:
            self.comboBox_MCU.setCurrentIndex(self.comboBox_MCU.findText(oldval_mcu))
        else:
            self.comboBox_MCU.setCurrentIndex(0)

        if self.comboBoxPython.findText(oldval_py) != -1:
            self.comboBoxPython.setCurrentIndex(self.comboBoxPython.findText(oldval_py))
        else:
            self.comboBoxPython.setCurrentIndex(0)
        self.assign_command()
        self.logger.info("Reloaded commands.")

    def reload_camera(self):
        self.capture_worker.set_fps(self.setting.setting["main_window"]["must"]["fps"])
        pass

    def screen_shot(self):
        try:
            self.capture_worker.saveCapture(capture_dir=self.cur_command.CAPTURE_DIR)
        except Exception as e:
            self.logger.error(e)
            pass
        pass

    def show_serial(self):
        if self.actionShow_Serial.isChecked():
            self.setting.setting["main_window"]["option"]["show_serial"] = True
            self.ser.is_show_serial = True
        else:
            self.setting.setting["main_window"]["option"]["show_serial"] = False
            self.ser.is_show_serial = False
        pass

    def capture_mousePressEvent(self, event):
        if event.modifiers() & QtCore.Qt.ControlModifier:  # Ctrlキーが押されているなら
            w = self.CaptureImageArea.width()
            h = self.CaptureImageArea.height()
            x = event.position().x()
            x_ = int(x * 1280 / w)
            y = event.position().y()
            y_ = int(y * 720 / h)
            c = self.img.pixel(x_, y_)
            # c_qobj = QColor
            c_rgb = QColor(c).getRgb()
            self.logger.debug(f"Clicked at x:{x_} y:{y_}, R:{c_rgb[0]} G:{c_rgb[1]} B: {c_rgb[2]}")
            return x, y, c_rgb

    def open_screen_shot_dir(self):
        os.startfile(os.path.realpath(self.cur_command.CAPTURE_DIR))

    def open_python_shot_dir(self):
        os.startfile(os.path.realpath(Path(self.cur_command.__directory__).resolve().parent))

    def open_mcu_shot_dir(self):
        os.startfile(os.path.realpath(self.cur_command.__directory__))

    def start_command(self):
        self.GamepadController_worker.pause = True
        self.assign_command()
        self.pushButton_PythonStart.setEnabled(False)
        # if self.thread_2 is not None:
        self.stop_command()
        if self.thread_2 is None:
            self.thread_2 = QThread()
        self.worker = self.cur_command()

        self.worker.moveToThread(self.thread_2)

        self.worker.print_strings.connect(self.callback_string_to_log, type=Qt.QueuedConnection)
        self.stop_request.connect(self.worker.stop, type=Qt.QueuedConnection)
        self.worker.stop_function.connect(self.callback_stop_command, type=Qt.QueuedConnection)

        self.worker.serial_input.connect(self.callback_keypress, type=Qt.DirectConnection)
        self.worker.get_image.connect(self.callback_return_img, type=Qt.DirectConnection)
        self.worker.recognize_rect.connect(self.callback_show_recognize_rect, type=Qt.DirectConnection)
        self.capture_worker.send_img.connect(self.worker.callback_receive_img, type=Qt.DirectConnection)
        self.worker.line_txt.connect(self.callback_line_txt, type=Qt.DirectConnection)
        self.worker.line_img.connect(self.callback_line_img, type=Qt.DirectConnection)
        self.worker.send_serial.connect(self.callback_run_macro, type=Qt.DirectConnection)

        self.thread_2.started.connect(self.worker.run)

        self.thread_2.finished.connect(self.worker.deleteLater)
        self.thread_2.finished.connect(self.thread_2.deleteLater)
        if self.worker is not None:
            try:
                self.worker.__post_init__()
            except:
                ...
        self.thread_2.start()

    @staticmethod
    def callback_start_command(self, is_alive: bool):
        try:
            if is_alive:
                print("ALIVE")
            else:
                print("DEAD")

        except Exception as e:
            print(e)
        pass

    def stop_command(self):
        if self.thread_2 and shiboken6.isValid(self.thread_2):
            self.logger.debug("Send Stop Requests.")
            # self.stop_request.emit(True)
            # スレッドが作成されていて、削除されていない
            if self.thread_2.isRunning() or not self.thread_2.isFinished():
                # self.worker.get_image.disconnect()
                # print("thread is stopping")
                self.worker.stop()
                self.thread_2.quit()
                self.thread_2.wait()
                # print("thread is stopped")
                self.worker = None
                self.thread_2 = None
                try:
                    self.GamepadController_worker.is_alive = True
                    self.GamepadController_worker.pause = False
                except:
                    self.logger.error("ERROR")
                # self.callback_stop_command()

    def start_mcu_command(self):
        self.assign_command()
        self.pushButton_MCUStart.setEnabled(False)
        self.pushButton_MCUStop.setEnabled(True)
        self._mcu_command = self.cur_command()
        self._mcu_command.play_sync_name.connect(self.play_mcu)
        self._mcu_command.start()
        pass

    def stop_mcu_command(self):
        self._mcu_command.end()
        self._mcu_command.play_sync_name.disconnect()
        self._mcu_command = None
        self.pushButton_MCUStart.setEnabled(True)
        self.pushButton_MCUStop.setEnabled(False)

    def play_mcu(self, s):
        self.ser.writeRow(s)
        pass

    @Slot(str, type(logging.DEBUG))
    def callback_string_to_log(self, s, level) -> None:
        match level:
            case logging.DEBUG:
                self.logger.debug(s)
            case logging.INFO:
                self.logger.info(s)
            case logging.WARNING:
                self.logger.warning(s)
            case logging.ERROR:
                self.logger.error(s)
            case logging.CRITICAL:
                self.logger.critical(s)
            case logging.FATAL:
                self.logger.fatal(s)

    @Slot(str, str)
    def callback_line_txt(self, s, token_key) -> None:
        self.line_notify.send_text(s, token_key=token_key)
        ...

    @Slot(str, str, np.ndarray)
    def callback_line_img(self, s, token_key, img) -> None:
        self.line_notify.send_text_n_image(img, s, token_key=token_key)
        ...

    def callback_stop_command(self):
        self.pushButton_PythonStart.setEnabled(True)
        self.stop_command()
        pass

    def open_python_commands_dir(self):
        pass

    def open_mcu_commands_dir(self):
        pass

    @Slot(type(Button.A), float, float)
    def callback_keypress(self, buttons, duration: float = 0.1, wait: float = 0.1, input_type: str = None):
        if self.keyPress is not None:
            match input_type:
                case "press":
                    self.keyPress.input(buttons)
                    self.wait(duration)
                    self.keyPress.inputEnd(buttons)
                    self.wait(wait)
                case "hold":
                    self.keyPress.hold(buttons)
                    self.wait(duration)
                case "hold end":
                    self.keyPress.holdEnd(buttons)
                case _:
                    self.logger.error("Something seems to have gone wrong.\nPlease send info to the developer.")

    @staticmethod
    def wait(wait):
        if float(wait) > 0.1:
            time.sleep(wait)
        else:
            current_time = time.perf_counter()
            while time.perf_counter() < current_time + wait:
                pass

    def callback_return_img(self):
        self.capture_worker.callback_return_img(True)
        pass

    def set_settings(self):

        try:
            self.logger.debug("Load setting")
            self.setting.load()
        except FileNotFoundError:
            self.logger.debug("File Not Found" "Generate setting")
            self.setting.generate()
            self.setting.load()

        self.lineEditFPS.setText(str(self.setting.setting["main_window"]["must"]["fps"]))
        self.lineEditCameraID.setText(str(self.setting.setting["main_window"]["must"]["camera_id"]))
        self.lineEditComPort.setText(str(self.setting.setting["main_window"]["must"]["com_port"]))
        self.actionShow_Serial.setChecked(self.setting.setting["main_window"]["option"]["show_serial"])

        self.keymap = {
                          v["assign"]: k for k, v in self.setting.setting["key_config"]["joystick"]["button"].items() if
                          v["state"]
                      } | {v["assign"]: k for k, v in self.setting.setting["key_config"]["joystick"]["hat"].items() if
                           v["state"]}

    def closeEvent(self, event: PySide6.QtGui.QCloseEvent) -> None:
        # Windowが最大化されていたら記憶する
        if self.isMaximized():
            self.setting.setting["main_window"]["option"]["window_showMaximized"] = True
        else:
            self.setting.setting["main_window"]["option"]["window_showMaximized"] = False
        self.setting.setting["command"]["py_command"] = self.comboBoxPython.currentText()
        self.setting.setting["command"]["mcu_command"] = self.comboBox_MCU.currentText()

        self.setting.save()
        try:
            if self.thread_1 is not None:
                self.thread_1.terminate()
            if self.thread_2 is not None:
                self.thread_2.terminate()
            self.GamepadController_worker.p.terminate()
            self.GamepadController_worker.p.join()
        except Exception:
            pass
        try:
            if self.GamepadController_worker is not None:
                self.GamepadController_worker.stop()
                self.GamepadController_worker.p.terminate()
                self.GamepadController_worker.p.join()
            if self.GamepadController_thread is not None:
                self.GamepadController_thread.terminate()
        except Exception as e:
            # print(000, e)
            pass

        self.logger.debug("Save settings")
        print("Save settings")
        return super().closeEvent(event)

    def callback_show_recognize_rect(self, t1: tuple, t2: tuple, color: QColor, frames: int = 120):
        if self.capture_worker is not None:
            self.capture_worker.rect_list.append([t1[0], t1[1], t2[0], t2[1], color, frames])

    @staticmethod
    def message_show_available_com_port():
        ls = serial_ports()
        ret = QMessageBox.information(None,
                                      "利用可能なCOMポート",
                                      f"利用可能なCOMポートは\n{','.join(ls)}\nです。",
                                      QMessageBox.Ok)


if __name__ == "__main__":
    logger = logging.Logger(__name__)
    # 環境変数にPySide6を登録
    dirname = os.path.dirname(PySide6.__file__)
    pluginPath = os.path.join(dirname, "plugins", "platforms")
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = pluginPath

    try:
        with open("ui/style.qss", "r") as f:
            style = f.read()
    except:
        style = ""
    # print(style)

    try:
        app = QApplication(sys.argv)
        app.setStyleSheet(style)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        # app = None
        logger.exception(e)

    print("quit")
