import copy
import sys
from datetime import date, datetime
from pathlib import Path

from loguru import logger
from PyQt5.QtCore import QDate, Qt, QThread, QStringListModel, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from login import (
    create_session,
    load_cookie_text,
    login_with_sms_code,
    request_sms_code,
    save_cookie_text,
    validate_cookie_text,
)
from order import place_order
from paths import get_runtime_file
from query import (
    enrich_ticket_prices,
    find_station,
    get_seat_value,
    has_enough_inventory,
    load_config,
    query_tickets,
    save_config,
    search_stations,
)


SEAT_OPTIONS = ["二等座", "一等座", "商务座"]
STEP_TITLES = ["步骤1 - Cookie", "步骤2 - 乘客管理", "步骤3 - 选票", "步骤4 - 抢票"]
SEGMENT_QUERY_FIELDS = ("from_station", "from_station_name", "to_station", "to_station_name", "train_date")
TRAIN_TYPE_FILTERS = ["高铁/动车", "普通车", "只看有票"]
TRAIN_GROUP_FILTERS = ["复兴号", "智能动车", "动感号"]
SEAT_TYPE_FILTERS = ["硬座无座", "二等座无座", "一等座", "二等座", "软卧", "特等座", "硬座", "一等卧", "二等卧", "商务座", "硬卧"]
SORT_OPTIONS = ["发时最早", "耗时最短", "价格最低"]
ONLY_SEAT_OPTIONS = ["全部", "只要二等座", "只要一等座", "只要商务座", "只要硬卧", "只要软卧", "只要硬座"]
FILTER_TO_SEAT_NAMES = {
    "硬座无座": ["硬座", "无座"],
    "二等座无座": ["二等座", "无座"],
    "一等座": ["一等座"],
    "二等座": ["二等座"],
    "软卧": ["软卧"],
    "特等座": ["商务座"],
    "硬座": ["硬座"],
    "一等卧": ["软卧"],
    "二等卧": ["硬卧"],
    "商务座": ["商务座"],
    "硬卧": ["硬卧"],
}
ALL_SEAT_NAMES = ["商务座", "一等座", "二等座", "软卧", "硬卧", "硬座", "无座"]
DISPLAY_SEAT_COLUMNS = ["二等座", "一等座", "商务座", "硬卧", "硬座"]
ONLY_SEAT_TO_NAME = {
    "只要二等座": "二等座",
    "只要一等座": "一等座",
    "只要商务座": "商务座",
    "只要硬卧": "硬卧",
    "只要软卧": "软卧",
    "只要硬座": "硬座",
}
COOKIE_FILE = get_runtime_file("cookies.txt")
CONFIG_FILE = get_runtime_file("config.yaml")


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)


def ensure_runtime_files():
    for file_path in (COOKIE_FILE, CONFIG_FILE):
        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")


def build_empty_segment(base_query: dict | None = None) -> dict:
    base_query = base_query or {}
    return {
        "from_station": base_query.get("from_station", ""),
        "from_station_name": base_query.get("from_station_name", ""),
        "to_station": base_query.get("to_station", ""),
        "to_station_name": base_query.get("to_station_name", ""),
        "train_date": base_query.get("train_date", date.today().strftime("%Y-%m-%d")),
        "primary": {},
        "backup": {},
    }


def normalize_segment(segment: dict | None, fallback_query: dict | None = None) -> dict:
    normalized = build_empty_segment(fallback_query)
    if isinstance(segment, dict):
        for field in SEGMENT_QUERY_FIELDS:
            if segment.get(field):
                normalized[field] = segment.get(field, "")
        for slot in ("primary", "backup"):
            value = segment.get(slot, {})
            normalized[slot] = copy.deepcopy(value) if isinstance(value, dict) else {}
    return normalized


def build_segment_runtime_config(app_config: dict, segment: dict) -> dict:
    runtime_config = copy.deepcopy(app_config)
    query_config = runtime_config.setdefault("query", {})
    for field in SEGMENT_QUERY_FIELDS:
        query_config[field] = segment.get(field, "")
    query_config["passenger_count"] = max(1, len(runtime_config.get("passengers", [])))
    runtime_config["selection"] = {
        "primary": copy.deepcopy(segment.get("primary", {})),
        "backup": copy.deepcopy(segment.get("backup", {})),
    }
    return runtime_config


def format_segment_route(segment: dict, index: int | None = None) -> str:
    prefix = f"第{index + 1}段 " if index is not None else ""
    from_name = segment.get("from_station_name") or segment.get("from_station") or "未设置"
    to_name = segment.get("to_station_name") or segment.get("to_station") or "未设置"
    train_date = segment.get("train_date") or "-"
    return f"{prefix}{from_name} → {to_name}  {train_date}"


def format_selection_text(selection: dict) -> str:
    if not selection:
        return "未选择"
    return (
        f"{selection.get('train_code', '-')}  "
        f"{selection.get('depart_time', '-')}→{selection.get('arrive_time', '-')}  "
        f"{selection.get('seat_name', '-')}"
    )


def parse_price_value(value: str | None) -> float:
    if not value:
        return float("inf")
    normalized = str(value).strip().replace("¥", "").replace("￥", "")
    try:
        return float(normalized)
    except ValueError:
        return float("inf")


def duration_to_minutes(duration: str | None) -> int:
    text = str(duration or "").strip()
    if not text or ":" not in text:
        return 10**9
    try:
        hours_text, minutes_text = text.split(":", 1)
        return int(hours_text) * 60 + int(minutes_text)
    except ValueError:
        return 10**9


def get_seat_price_key(seat_name: str) -> str:
    return {
        "二等座": "second_class_price",
        "一等座": "first_class_price",
        "商务座": "business_class_price",
        "硬卧": "hard_sleeper_price",
        "硬座": "hard_seat_price",
        "软卧": "soft_sleeper_price",
        "无座": "no_seat_price",
    }.get(seat_name, "")


def get_seat_price(ticket: dict, seat_name: str) -> str:
    key = get_seat_price_key(seat_name)
    return ticket.get(key, "-") if key else "-"


def get_seat_display_state(ticket: dict, seat_name: str) -> tuple[str, str]:
    inventory = get_seat_value(ticket, seat_name)
    price = get_seat_price(ticket, seat_name)
    if inventory == "候补":
        return "候补", "waitlist"
    if not inventory or inventory in {"无", "--", "*"}:
        return "--", "empty"
    price_text = price if price and price != "-" else ""
    return (f"{inventory}\n{price_text}".strip(), "available")


def ticket_has_any_inventory(ticket: dict) -> bool:
    for seat_name in ALL_SEAT_NAMES:
        if has_enough_inventory(get_seat_value(ticket, seat_name), 1):
            return True
    return False


def ticket_lowest_available_price(ticket: dict) -> float:
    prices = []
    for seat_name in ALL_SEAT_NAMES:
        if has_enough_inventory(get_seat_value(ticket, seat_name), 1):
            prices.append(parse_price_value(get_seat_price(ticket, seat_name)))
    return min(prices) if prices else float("inf")


def get_train_type_label(ticket: dict) -> str:
    train_code = str(ticket.get("train_code", "")).upper()
    if train_code.startswith(("G", "D", "C")):
        return "高铁/动车"
    return "普通车"


def matches_train_group(ticket: dict, group_name: str) -> bool:
    train_code = str(ticket.get("train_code", "")).upper()
    if group_name == "复兴号":
        return train_code.startswith(("G", "C"))
    if group_name == "智能动车":
        return train_code.startswith(("G", "D"))
    if group_name == "动感号":
        return train_code.startswith("D")
    return False


def matches_seat_filter(ticket: dict, filter_name: str) -> bool:
    for seat_name in FILTER_TO_SEAT_NAMES.get(filter_name, []):
        if get_seat_value(ticket, seat_name) not in {"", "无", "--", "*"}:
            return True
    return False


def selection_matches_ticket(selection: dict, ticket: dict) -> bool:
    return (
        selection.get("train_code") == ticket.get("train_code")
        and selection.get("depart_time") == ticket.get("depart_time")
        and selection.get("arrive_time") == ticket.get("arrive_time")
    )


class CookieValidateWorker(QThread):
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, cookie_text: str):
        super().__init__()
        self.cookie_text = cookie_text

    def run(self):
        session = create_session()
        is_valid, _ = validate_cookie_text(self.cookie_text, session=session)
        if is_valid:
            self.finished_signal.emit(True, "登录有效")
        else:
            self.finished_signal.emit(False, "Cookie已过期")


class SmsCodeWorker(QThread):
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, username: str, password: str, id_suffix: str, session):
        super().__init__()
        self.username = username
        self.password = password
        self.id_suffix = id_suffix
        self.session = session

    def run(self):
        success, message, _ = request_sms_code(
            username=self.username,
            password=self.password,
            id_suffix=self.id_suffix,
            session=self.session,
        )
        self.finished_signal.emit(success, message)


class AccountLoginWorker(QThread):
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, username: str, password: str, sms_code: str, id_suffix: str, session):
        super().__init__()
        self.username = username
        self.password = password
        self.sms_code = sms_code
        self.id_suffix = id_suffix
        self.session = session

    def run(self):
        success, message, _ = login_with_sms_code(
            username=self.username,
            password=self.password,
            sms_code=self.sms_code,
            id_suffix=self.id_suffix,
            session=self.session,
        )
        self.finished_signal.emit(success, message)


class QueryWorker(QThread):
    result_signal = pyqtSignal(int, int, object, bool)
    error_signal = pyqtSignal(int, str)

    def __init__(self, request_id: int, segment_index: int, runtime_config: dict):
        super().__init__()
        self.request_id = request_id
        self.segment_index = segment_index
        self.runtime_config = copy.deepcopy(runtime_config)

    def run(self):
        session = create_session()
        is_valid, _ = validate_cookie_text(load_cookie_text(), session=session)
        if not is_valid:
            self.error_signal.emit(self.request_id, "Cookie 无效，请先回到步骤1重新验证")
            return
        trains = query_tickets(
            session=session,
            config=self.runtime_config,
            verbose=False,
            include_prices=False,
            debug=True,
        )
        trains.sort(key=lambda item: item.get("depart_time", ""))
        self.result_signal.emit(self.request_id, self.segment_index, copy.deepcopy(trains), False)
        if not trains:
            return
        train_date = self.runtime_config.get("query", {}).get("train_date", "")
        enrich_ticket_prices(session, trains, train_date)
        self.result_signal.emit(self.request_id, self.segment_index, copy.deepcopy(trains), True)


class TicketingWorker(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str, str, str)
    active_target_signal = pyqtSignal(str)
    success_signal = pyqtSignal(str)
    failure_signal = pyqtSignal(str)

    def __init__(self, config_data: dict):
        super().__init__()
        self.config_data = copy.deepcopy(config_data)
        self.stop_requested = False

    def stop(self):
        self.stop_requested = True

    def _emit_log(self, message: str):
        logger.info(message)
        self.log_signal.emit(message)

    def _find_ticket(self, trains: list[dict], selection: dict):
        for train in trains:
            if (
                train.get("train_code") == selection.get("train_code")
                and train.get("depart_time") == selection.get("depart_time")
                and train.get("arrive_time") == selection.get("arrive_time")
            ):
                return train
        return None

    def _wait_with_stop(self, seconds: int) -> bool:
        steps = max(1, int(seconds * 10))
        for _ in range(steps):
            if self.stop_requested:
                return False
            self.msleep(100)
        return True

    def _run_single_segment(self, session, segment: dict, segment_index: int, interval: int, passenger_count: int) -> dict:
        current_slot = "primary"
        preferred_attempts = 0
        route_text = format_segment_route(segment, segment_index - 1)

        while not self.stop_requested:
            runtime_config = build_segment_runtime_config(self.config_data, segment)
            target = segment.get(current_slot, {})
            seat_name = target.get("seat_name", "二等座")
            slot_title = "首选车票" if current_slot == "primary" else "保底车票"
            self.active_target_signal.emit(f"当前目标：第{segment_index}段 {slot_title}")

            trains = query_tickets(session=session, config=runtime_config, verbose=False)
            if not trains:
                self._emit_log(f"第{segment_index}段 {route_text} 本轮没有获取到有效车次列表")
                if not self._wait_with_stop(interval):
                    break
                continue

            target_train = self._find_ticket(trains, target)
            if not target_train:
                self._emit_log(f"第{segment_index}段 {slot_title} 未在当前车次列表中出现")
                if current_slot == "primary":
                    preferred_attempts += 1
                    if preferred_attempts >= 20:
                        current_slot = "backup"
                        self._emit_log(f"第{segment_index}段首选连续查询20次未成功，已切换到保底车票")
                if not self._wait_with_stop(interval):
                    break
                continue

            seat_value = get_seat_value(target_train, seat_name) or "-"
            self._emit_log(
                f"第{segment_index}段 {slot_title} "
                f"{target_train.get('train_code')} {target_train.get('depart_time')}->{target_train.get('arrive_time')} "
                f"{seat_name}余票：{seat_value}"
            )

            if not has_enough_inventory(seat_value, passenger_count):
                if current_slot == "primary":
                    preferred_attempts += 1
                    if preferred_attempts >= 20:
                        current_slot = "backup"
                        self._emit_log(f"第{segment_index}段首选连续查询20次未抢到，开始尝试保底车票")
                if not self._wait_with_stop(interval):
                    break
                continue

            self.status_signal.emit(f"第{segment_index}段抢票进行中", "#1d4ed8", "#dbeafe")
            self._emit_log(f"第{segment_index}段命中可下单余票，开始尝试抢 {seat_name}")
            result = place_order(
                session=session,
                config=runtime_config,
                ticket=target_train,
                seat_name=seat_name,
                log_callback=self._emit_log,
            )
            if result.get("success"):
                return {
                    "success": True,
                    "message": (
                        f"第{segment_index}段抢票成功："
                        f"{target_train.get('train_code')} {target_train.get('depart_time')}->{target_train.get('arrive_time')} {seat_name}"
                    ),
                }

            self._emit_log(result["message"])
            if not result.get("retryable", True):
                return {
                    "success": False,
                    "message": f"第{segment_index}段未抢到，请手动处理",
                    "detail": result["message"],
                }

            if current_slot == "primary":
                preferred_attempts += 1
                if preferred_attempts >= 20:
                    current_slot = "backup"
                    self._emit_log(f"第{segment_index}段首选连续查询20次未抢到，开始尝试保底车票")
            if not self._wait_with_stop(interval):
                break

        return {"success": False, "stopped": True, "message": "已手动停止抢票"}

    def run(self):
        session = create_session()
        is_valid, _ = validate_cookie_text(load_cookie_text(), session=session)
        if not is_valid:
            self.status_signal.emit("等待放票中", "#b91c1c", "#fee2e2")
            self.failure_signal.emit("Cookie 无效，请先回到步骤1重新验证")
            return

        segments = self.config_data.get("segments", [])
        passenger_count = max(1, len(self.config_data.get("passengers", [])))
        interval = int(self.config_data.get("schedule", {}).get("interval_seconds", 4))

        self.status_signal.emit("等待放票中", "#c2410c", "#fff7ed")
        self._emit_log("抢票线程已启动，将按行程段顺序依次尝试")

        for segment_index, segment in enumerate(segments, start=1):
            if self.stop_requested:
                self.failure_signal.emit("已手动停止抢票")
                return
            self._emit_log(f"开始处理第{segment_index}段：{format_segment_route(segment, segment_index - 1)}")
            result = self._run_single_segment(session, segment, segment_index, interval, passenger_count)
            if result.get("stopped"):
                self.failure_signal.emit("已手动停止抢票")
                return
            if not result.get("success"):
                self._emit_log(result.get("detail", result["message"]))
                self._emit_log(result["message"])
                self.status_signal.emit(result["message"], "#b91c1c", "#fee2e2")
                self.failure_signal.emit(result["message"])
                return
            self._emit_log(result["message"])
            if segment_index < len(segments):
                self.status_signal.emit(f"第{segment_index}段已成功，继续抢下一段", "#166534", "#dcfce7")

        if not self.stop_requested:
            self.status_signal.emit("抢票成功", "#166534", "#dcfce7")
            self.success_signal.emit("🎉 抢票成功！请30分钟内支付")


class StationInput(QWidget):
    def __init__(self, label_text: str):
        super().__init__()
        self.selected_station = None
        self.matches = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.label = QLabel(label_text)
        self.label.setObjectName("fieldLabel")
        layout.addWidget(self.label)

        self.line_edit = QLineEdit()
        self.line_edit.textEdited.connect(self._on_text_edited)
        self.line_edit.editingFinished.connect(self.resolve_station)
        layout.addWidget(self.line_edit)

        self.model = QStringListModel()
        self.completer = QCompleter(self.model, self)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.activated[str].connect(self._on_completer_activated)
        self.line_edit.setCompleter(self.completer)

    def _on_text_edited(self, text: str):
        keyword = text.strip()
        self.selected_station = None
        if not keyword:
            self.matches = []
            self.model.setStringList([])
            return
        self.matches = search_stations(keyword, limit=8)
        self.model.setStringList([f"{station['name']} ({station['code']})" for station in self.matches])

    def _on_completer_activated(self, text: str):
        for station in self.matches:
            if text == f"{station['name']} ({station['code']})":
                self.selected_station = station
                self.line_edit.setText(station["name"])
                return

    def set_station(self, station_name: str, station_code: str = ""):
        station = find_station(station_code or station_name)
        if station:
            self.selected_station = station
            self.line_edit.setText(station["name"])
        else:
            self.selected_station = None
            self.line_edit.setText(station_name)

    def resolve_station(self):
        if self.selected_station:
            return self.selected_station
        station = find_station(self.line_edit.text())
        if station:
            self.selected_station = station
            self.line_edit.setText(station["name"])
        return station


class AddPassengerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加乘客")
        self.setModal(True)
        self.resize(360, 160)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.id_input = QLineEdit()
        form.addRow("姓名", self.name_input)
        form.addRow("身份证号", self.id_input)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        confirm = QPushButton("确认加入列表")
        confirm.setObjectName("primaryButton")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

    def get_values(self) -> tuple[str, str]:
        return self.name_input.text().strip(), self.id_input.text().strip()


class CookiePage(QWidget):
    def __init__(self, app_window):
        super().__init__()
        self.app_window = app_window
        self.cookie_worker = None
        self.sms_code_worker = None
        self.account_login_worker = None
        self.account_session = None
        self.sms_countdown_seconds = 0
        self.sms_timer = QTimer(self)
        self.sms_timer.timeout.connect(self._update_sms_countdown)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("步骤1 - 登录")
        title.setObjectName("pageTitle")
        hint = QLabel("支持账号密码登录或直接粘贴 Cookie。登录成功后会自动保存到 cookies.txt。")
        hint.setObjectName("pageHint")
        layout.addWidget(title)
        layout.addWidget(hint)

        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._build_account_login_tab(), "账号登录")
        self.tab_widget.addTab(self._build_cookie_login_tab(), "Cookie登录")
        layout.addWidget(self.tab_widget)
        layout.addStretch(1)

    def on_show(self):
        self.cookie_edit.setPlainText(load_cookie_text())

    def _reset_account_session(self):
        self.account_session = create_session()
        return self.account_session

    def _build_account_login_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        form = QFormLayout()
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("请输入12306账号/手机号")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.id_suffix_input = QLineEdit()
        self.id_suffix_input.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.id_suffix_input.setMaxLength(4)
        self.id_suffix_input.setPlaceholderText("证件号后4位")
        self.sms_code_input = QLineEdit()
        self.sms_code_input.setMaxLength(6)
        self.sms_code_input.setPlaceholderText("请输入6位短信验证码")
        form.addRow("手机号", self.username_input)
        form.addRow("密码", self.password_input)
        form.addRow("证件号后4位", self.id_suffix_input)
        form.addRow("短信验证码", self.sms_code_input)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        self.get_sms_code_button = QPushButton("获取验证码")
        self.get_sms_code_button.setObjectName("secondaryButton")
        self.get_sms_code_button.clicked.connect(self.request_sms_code)
        self.account_login_button = QPushButton("登录")
        self.account_login_button.setObjectName("primaryButton")
        self.account_login_button.clicked.connect(self.login_with_account)
        button_row.addWidget(self.get_sms_code_button)
        button_row.addWidget(self.account_login_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.account_status_label = QLabel("")
        self.account_status_label.setObjectName("statusText")
        layout.addWidget(self.account_status_label)
        layout.addStretch(1)
        return tab

    def _build_cookie_login_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setPlaceholderText("请粘贴完整 Cookie 字符串")
        self.cookie_edit.setMinimumHeight(220)
        layout.addWidget(self.cookie_edit)

        button_row = QHBoxLayout()
        self.verify_button = QPushButton("验证并继续")
        self.verify_button.setObjectName("primaryButton")
        self.verify_button.clicked.connect(self.validate_cookie)
        button_row.addWidget(self.verify_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.cookie_status_label = QLabel("")
        self.cookie_status_label.setObjectName("statusText")
        layout.addWidget(self.cookie_status_label)
        layout.addStretch(1)
        return tab

    def set_cookie_status(self, text: str, color: str):
        self.cookie_status_label.setText(text)
        self.cookie_status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.verify_button.setEnabled(True)

    def set_account_status(self, text: str, color: str):
        self.account_status_label.setText(text)
        self.account_status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.account_login_button.setEnabled(True)
        if self.sms_countdown_seconds == 0:
            self.get_sms_code_button.setEnabled(True)

    def _update_sms_countdown(self):
        if self.sms_countdown_seconds <= 0:
            self.sms_timer.stop()
            self.get_sms_code_button.setEnabled(True)
            self.get_sms_code_button.setText("获取验证码")
            return
        self.get_sms_code_button.setText(f"{self.sms_countdown_seconds}秒后重试")
        self.sms_countdown_seconds -= 1

    def request_sms_code(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        id_suffix = self.id_suffix_input.text().strip()
        if not username or not password:
            self.set_account_status("请先填写手机号/账号和密码", "#dc2626")
            return
        if len(id_suffix) != 4:
            self.set_account_status("请输入证件号后4位", "#dc2626")
            return
        self.get_sms_code_button.setEnabled(False)
        self.account_login_button.setEnabled(False)
        self.set_account_status("正在获取短信验证码，请稍候...", "#2563eb")
        session = self._reset_account_session()
        self.sms_code_worker = SmsCodeWorker(username, password, id_suffix, session)
        self.sms_code_worker.finished_signal.connect(self._on_sms_code_finished)
        self.sms_code_worker.start()

    def _on_sms_code_finished(self, success: bool, message: str):
        self.account_login_button.setEnabled(True)
        if success:
            self.sms_countdown_seconds = 60
            self._update_sms_countdown()
            self.sms_timer.start(1000)
            self.set_account_status(message or "短信验证码已发送", "#16a34a")
            return
        self.get_sms_code_button.setEnabled(True)
        self.get_sms_code_button.setText("获取验证码")
        self.set_account_status(message or "获取短信验证码失败", "#dc2626")

    def login_with_account(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        sms_code = self.sms_code_input.text().strip()
        id_suffix = self.id_suffix_input.text().strip()
        if not username or not password or not sms_code:
            self.set_account_status("请填写手机号、密码和短信验证码", "#dc2626")
            return
        if len(id_suffix) != 4:
            self.set_account_status("请输入证件号后4位", "#dc2626")
            return
        if len(sms_code) != 6 or not sms_code.isdigit():
            self.set_account_status("短信验证码应为6位数字", "#dc2626")
            return
        if self.account_session is None:
            self.set_account_status("请先点击获取验证码，保持同一次登录会话", "#dc2626")
            return
        self.account_login_button.setEnabled(False)
        self.get_sms_code_button.setEnabled(False)
        self.set_account_status("正在登录，请稍候...", "#2563eb")
        self.account_login_worker = AccountLoginWorker(
            username,
            password,
            sms_code,
            id_suffix,
            self.account_session,
        )
        self.account_login_worker.finished_signal.connect(self._on_account_login_finished)
        self.account_login_worker.start()

    def validate_cookie(self):
        cookie_text = self.cookie_edit.toPlainText().strip()
        if not cookie_text:
            self.set_cookie_status("请先粘贴 Cookie", "#dc2626")
            return
        save_cookie_text(cookie_text)
        self.verify_button.setEnabled(False)
        self.cookie_status_label.setText("正在验证，请稍候...")
        self.cookie_status_label.setStyleSheet("color: #2563eb; font-weight: 600;")

        self.cookie_worker = CookieValidateWorker(cookie_text)
        self.cookie_worker.finished_signal.connect(self._on_validation_finished)
        self.cookie_worker.start()

    def _on_validation_finished(self, is_valid: bool, text: str):
        if is_valid:
            self.set_cookie_status(text, "#16a34a")
            self.app_window.show_step(1)
        else:
            self.set_cookie_status(text, "#dc2626")

    def _on_account_login_finished(self, success: bool, message: str):
        if success:
            self.sms_timer.stop()
            self.sms_countdown_seconds = 0
            self.get_sms_code_button.setText("获取验证码")
            self.set_account_status(message, "#16a34a")
            self.cookie_edit.setPlainText(load_cookie_text())
            self.app_window.show_step(1)
            return
        self.set_account_status(message, "#dc2626")
        if self.sms_countdown_seconds == 0:
            self.get_sms_code_button.setEnabled(True)


class PassengerPage(QWidget):
    def __init__(self, app_window):
        super().__init__()
        self.app_window = app_window

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("步骤2 - 乘客管理")
        title.setObjectName("pageTitle")
        hint = QLabel("乘客数据会保存到 config.yaml。")
        hint.setObjectName("pageHint")
        layout.addWidget(title)
        layout.addWidget(hint)

        action_row = QHBoxLayout()
        add_button = QPushButton("添加乘客")
        add_button.setObjectName("secondaryButton")
        add_button.clicked.connect(self.open_add_dialog)
        next_button = QPushButton("下一步")
        next_button.setObjectName("primaryButton")
        next_button.clicked.connect(lambda: self.app_window.show_step(2))
        action_row.addWidget(add_button)
        action_row.addStretch(1)
        action_row.addWidget(next_button)
        layout.addLayout(action_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["姓名", "身份证号", "操作"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

    def on_show(self):
        self.refresh_table()

    def refresh_table(self):
        passengers = self.app_window.config_data.get("passengers", [])
        if not passengers:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("暂未添加乘客"))
            self.table.setItem(0, 1, QTableWidgetItem(""))
            self.table.setCellWidget(0, 2, QWidget())
            return

        self.table.setRowCount(len(passengers))
        for row, passenger in enumerate(passengers):
            self.table.setItem(row, 0, QTableWidgetItem(passenger.get("name", "")))
            self.table.setItem(row, 1, QTableWidgetItem(self.app_window.mask_id_card(passenger.get("id_card", ""))))
            delete_button = QPushButton("删除")
            delete_button.setObjectName("dangerButton")
            delete_button.clicked.connect(lambda _checked=False, index=row: self.delete_passenger(index))
            self.table.setCellWidget(row, 2, delete_button)

    def delete_passenger(self, index: int):
        passengers = self.app_window.config_data.get("passengers", [])
        if 0 <= index < len(passengers):
            passengers.pop(index)
            self.app_window.config_data.setdefault("query", {})["passenger_count"] = max(1, len(passengers))
            self.app_window.save_config_data()
            self.refresh_table()

    def open_add_dialog(self):
        dialog = AddPassengerDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        name, id_card = dialog.get_values()
        if not name or not id_card:
            QMessageBox.warning(self, "提示", "姓名和身份证号都不能为空")
            return
        self.app_window.config_data.setdefault("passengers", []).append({"name": name, "id_card": id_card})
        self.app_window.config_data.setdefault("query", {})["passenger_count"] = len(self.app_window.config_data["passengers"])
        self.app_window.save_config_data()
        self.refresh_table()


class TicketSelectionPage(QWidget):
    def __init__(self, app_window):
        super().__init__()
        self.app_window = app_window
        self.current_segment_index = 0
        self.segment_tickets: dict[int, list[dict]] = {}
        self.selected_seats: dict[int, dict[str, str]] = {}
        self.query_worker = None
        self.active_train_type_filters: set[str] = set()
        self.active_train_group_filters: set[str] = set()
        self.active_seat_filters: set[str] = set()
        self.active_sort = "发时最早"
        self.active_only_seat = "全部"
        self.query_request_id = 0
        self.active_query_request_id = 0
        self.query_elapsed_seconds = 0
        self.query_timed_out = False
        self.query_timer = QTimer(self)
        self.query_timer.timeout.connect(self.update_query_elapsed)
        self.query_timeout_timer = QTimer(self)
        self.query_timeout_timer.setSingleShot(True)
        self.query_timeout_timer.timeout.connect(self.on_query_timeout)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("步骤3 - 选票")
        title.setObjectName("pageTitle")
        hint = QLabel("支持任意城市搜索和中转分段。每一段都可以分别设置首选和保底车票。")
        hint.setObjectName("pageHint")
        layout.addWidget(title)
        layout.addWidget(hint)

        segment_row = QHBoxLayout()
        self.segment_tabs_layout = QHBoxLayout()
        self.segment_tabs_layout.setSpacing(8)
        segment_tabs_widget = QWidget()
        segment_tabs_widget.setLayout(self.segment_tabs_layout)
        segment_row.addWidget(segment_tabs_widget, 1)

        self.remove_segment_button = QPushButton("删除当前段")
        self.remove_segment_button.setObjectName("dangerButton")
        self.remove_segment_button.clicked.connect(self.remove_current_segment)
        add_segment_button = QPushButton("添加中转")
        add_segment_button.setObjectName("secondaryButton")
        add_segment_button.clicked.connect(self.add_segment)
        segment_row.addWidget(self.remove_segment_button)
        segment_row.addWidget(add_segment_button)
        layout.addLayout(segment_row)

        self.current_segment_label = QLabel("当前编辑：第1段")
        self.current_segment_label.setObjectName("activePill")
        layout.addWidget(self.current_segment_label)

        status_row = QHBoxLayout()
        self.current_primary_label = QLabel("首选车票：未选择")
        self.current_primary_label.setObjectName("selectionStatusPrimary")
        self.current_backup_label = QLabel("保底车票：未选择")
        self.current_backup_label.setObjectName("selectionStatusBackup")
        status_row.addWidget(self.current_primary_label, 1)
        status_row.addWidget(self.current_backup_label, 1)
        layout.addLayout(status_row)

        form_row = QHBoxLayout()
        self.from_input = StationInput("出发地")
        self.to_input = StationInput("目的地")
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("yyyy-MM-dd")
        date_wrap = QWidget()
        date_layout = QVBoxLayout(date_wrap)
        date_layout.setContentsMargins(0, 0, 0, 0)
        date_layout.setSpacing(6)
        date_label = QLabel("出发日期")
        date_label.setObjectName("fieldLabel")
        date_layout.addWidget(date_label)
        date_layout.addWidget(self.date_input)
        form_row.addWidget(self.from_input, 1)
        form_row.addWidget(self.to_input, 1)
        form_row.addWidget(date_wrap, 1)
        layout.addLayout(form_row)

        action_row = QHBoxLayout()
        self.query_button = QPushButton("查询当前段")
        self.query_button.setObjectName("primaryButton")
        self.query_button.clicked.connect(self.refresh_tickets)
        self.query_status_label = QLabel("")
        self.query_status_label.setObjectName("pageHint")
        self.start_button = QPushButton("开始抢票")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_ticketing)
        action_row.addWidget(self.query_button)
        action_row.addWidget(self.query_status_label)
        action_row.addStretch(1)
        action_row.addWidget(self.start_button)
        layout.addLayout(action_row)

        self.notice_label = QLabel("请先选择一段行程并查询车次。")
        self.notice_label.setObjectName("pageHint")
        layout.addWidget(self.notice_label)

        self.result_count_label = QLabel("共0趟 · 已筛选0趟")
        self.result_count_label.setObjectName("pageHint")
        layout.addWidget(self.result_count_label)

        filter_box = QFrame()
        filter_box.setObjectName("summaryCard")
        filter_layout = QVBoxLayout(filter_box)
        filter_layout.setContentsMargins(12, 12, 12, 12)
        filter_layout.setSpacing(10)
        self.filter_buttons = {}
        self.sort_buttons = {}
        filter_layout.addLayout(self._build_filter_group("车次类型", TRAIN_TYPE_FILTERS, self.on_train_type_filter_changed))
        filter_layout.addLayout(self._build_filter_group("车组类型", TRAIN_GROUP_FILTERS, self.on_train_group_filter_changed))
        filter_layout.addLayout(self._build_filter_group("席别类型", SEAT_TYPE_FILTERS, self.on_seat_filter_changed))
        filter_layout.addLayout(self._build_only_seat_group())
        filter_layout.addLayout(self._build_sort_group())
        layout.addWidget(filter_box)

        self.summary_scroll = QScrollArea()
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.summary_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.summary_scroll.setFrameShape(QFrame.NoFrame)
        self.summary_container = QWidget()
        self.summary_layout = QHBoxLayout(self.summary_container)
        self.summary_layout.setContentsMargins(0, 0, 0, 0)
        self.summary_layout.setSpacing(10)
        self.summary_scroll.setWidget(self.summary_container)
        layout.addWidget(self.summary_scroll)

        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            ["车次", "出发/到达", "历时", "二等座", "一等座", "商务座", "硬卧", "硬座", "席别选择", "首选", "保底"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2, 3, 4, 5, 6, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(10, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

    def on_show(self):
        segments = self.app_window.get_segments()
        if self.current_segment_index >= len(segments):
            self.current_segment_index = 0
        self.refresh_segment_tabs()
        self.load_current_segment_fields()
        self.render_segment_summary()
        tickets = self.segment_tickets.get(self.current_segment_index, [])
        if tickets:
            self.apply_filters_and_sort()
        else:
            self.notice_label.setText("请先查询当前段车次。")
            self.result_count_label.setText("共0趟 · 已筛选0趟")
            self.table.setRowCount(0)

    def _get_current_segment(self) -> dict:
        return self.app_window.get_segments()[self.current_segment_index]

    def _ticket_key(self, ticket: dict) -> str:
        return f"{ticket.get('train_code')}|{ticket.get('depart_time')}|{ticket.get('arrive_time')}"

    def _build_price_text(self, ticket: dict, seat_name: str) -> str:
        return get_seat_price(ticket, seat_name) or "-"

    def _build_filter_group(self, title: str, options: list[str], handler):
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("fieldLabel")
        row.addWidget(label)
        for option in options:
            button = QPushButton(option)
            button.setObjectName("filterChip")
            button.setCheckable(True)
            button.toggled.connect(lambda checked, name=option: handler(name, checked))
            self.filter_buttons[option] = button
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _build_sort_group(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("排序")
        label.setObjectName("fieldLabel")
        row.addWidget(label)
        for option in SORT_OPTIONS:
            button = QPushButton(option)
            button.setObjectName("sortChip")
            button.setCheckable(True)
            button.setChecked(option == self.active_sort)
            button.toggled.connect(lambda checked, name=option: self.on_sort_changed(name, checked))
            self.sort_buttons[option] = button
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _build_only_seat_group(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("只看席别")
        label.setObjectName("fieldLabel")
        row.addWidget(label)
        self.only_seat_buttons = {}
        for option in ONLY_SEAT_OPTIONS:
            button = QPushButton(option)
            button.setObjectName("sortChip")
            button.setCheckable(True)
            button.setChecked(option == self.active_only_seat)
            button.toggled.connect(lambda checked, name=option: self.on_only_seat_changed(name, checked))
            self.only_seat_buttons[option] = button
            row.addWidget(button)
        row.addStretch(1)
        return row

    def on_train_type_filter_changed(self, name: str, checked: bool):
        if checked:
            self.active_train_type_filters.add(name)
        else:
            self.active_train_type_filters.discard(name)
        self.apply_filters_and_sort()

    def on_train_group_filter_changed(self, name: str, checked: bool):
        if checked:
            self.active_train_group_filters.add(name)
        else:
            self.active_train_group_filters.discard(name)
        self.apply_filters_and_sort()

    def on_seat_filter_changed(self, name: str, checked: bool):
        if checked:
            self.active_seat_filters.add(name)
        else:
            self.active_seat_filters.discard(name)
        self.apply_filters_and_sort()

    def on_sort_changed(self, name: str, checked: bool):
        if not checked:
            if self.active_sort == name and name in self.sort_buttons:
                self.sort_buttons[name].blockSignals(True)
                self.sort_buttons[name].setChecked(True)
                self.sort_buttons[name].blockSignals(False)
            return
        self.active_sort = name
        for option, button in self.sort_buttons.items():
            if option != name and button.isChecked():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
        self.apply_filters_and_sort()

    def on_only_seat_changed(self, name: str, checked: bool):
        if not checked:
            if self.active_only_seat == name and name in self.only_seat_buttons:
                self.only_seat_buttons[name].blockSignals(True)
                self.only_seat_buttons[name].setChecked(True)
                self.only_seat_buttons[name].blockSignals(False)
            return
        self.active_only_seat = name
        for option, button in self.only_seat_buttons.items():
            if option != name and button.isChecked():
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
        self.apply_filters_and_sort()

    def update_query_elapsed(self):
        self.query_elapsed_seconds += 1
        self.query_status_label.setText(f"查询中... {self.query_elapsed_seconds}秒")
        self.query_status_label.setStyleSheet("color: #1d4ed8; font-weight: 600;")

    def start_query_feedback(self):
        self.query_elapsed_seconds = 0
        self.query_timed_out = False
        self.query_button.setEnabled(False)
        self.query_status_label.setText("查询中... 0秒")
        self.query_status_label.setStyleSheet("color: #1d4ed8; font-weight: 600;")
        self.query_timer.start(1000)
        self.query_timeout_timer.start(15000)

    def stop_query_feedback(self):
        self.query_timer.stop()
        self.query_timeout_timer.stop()
        self.query_button.setEnabled(True)

    def on_query_timeout(self):
        self.query_timed_out = True
        self.stop_query_feedback()
        self.query_status_label.setText("查询超时，请检查网络或Cookie是否有效")
        self.query_status_label.setStyleSheet("color: #dc2626; font-weight: 700;")
        self.notice_label.setText("查询超时，请检查网络或Cookie是否有效")

    def refresh_segment_tabs(self):
        clear_layout(self.segment_tabs_layout)
        segments = self.app_window.get_segments()
        for index, segment in enumerate(segments):
            route = f"{segment.get('from_station_name') or '未设'} → {segment.get('to_station_name') or '未设'}"
            button = QPushButton(f"第{index + 1}段\n{route}")
            button.setObjectName("segmentTab")
            button.setProperty("active", index == self.current_segment_index)
            button.clicked.connect(lambda _checked=False, i=index: self.switch_segment(i))
            self.segment_tabs_layout.addWidget(button)
        self.segment_tabs_layout.addStretch(1)
        self.remove_segment_button.setEnabled(len(segments) > 1)

    def render_segment_summary(self):
        clear_layout(self.summary_layout)
        for index, segment in enumerate(self.app_window.get_segments()):
            card = QFrame()
            card.setObjectName("summaryCard")
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            card_layout = QVBoxLayout(card)
            route_label = QLabel(format_segment_route(segment, index))
            route_label.setObjectName("cardTitle")
            primary_label = QLabel(f"首选：{format_selection_text(segment.get('primary', {}))}")
            backup_label = QLabel(f"保底：{format_selection_text(segment.get('backup', {}))}")
            primary_label.setWordWrap(True)
            backup_label.setWordWrap(True)
            card_layout.addWidget(route_label)
            card_layout.addWidget(primary_label)
            card_layout.addWidget(backup_label)
            self.summary_layout.addWidget(card)
        self.summary_layout.addStretch(1)

    def load_current_segment_fields(self):
        segment = self._get_current_segment()
        self.current_segment_label.setText(f"当前编辑：{format_segment_route(segment, self.current_segment_index)}")
        self.from_input.set_station(segment.get("from_station_name", ""), segment.get("from_station", ""))
        self.to_input.set_station(segment.get("to_station_name", ""), segment.get("to_station", ""))
        try:
            current_date = datetime.strptime(segment.get("train_date", ""), "%Y-%m-%d").date()
        except ValueError:
            current_date = date.today()
        self.date_input.setDate(QDate(current_date.year, current_date.month, current_date.day))
        self.update_current_selection_status()

    def update_current_selection_status(self):
        segment = self._get_current_segment()
        self.current_primary_label.setText(f"首选车票：{format_selection_text(segment.get('primary', {}))}")
        self.current_backup_label.setText(f"保底车票：{format_selection_text(segment.get('backup', {}))}")

    def _matches_filters(self, ticket: dict) -> bool:
        if self.active_train_type_filters:
            matched_type_filters = set(self.active_train_type_filters)
            if "只看有票" in matched_type_filters:
                matched_type_filters.discard("只看有票")
                if not ticket_has_any_inventory(ticket):
                    return False
            if matched_type_filters and get_train_type_label(ticket) not in matched_type_filters:
                return False

        if self.active_train_group_filters and not any(
            matches_train_group(ticket, group_name) for group_name in self.active_train_group_filters
        ):
            return False

        if self.active_seat_filters and not any(
            matches_seat_filter(ticket, seat_filter) for seat_filter in self.active_seat_filters
        ):
            return False

        if self.active_only_seat != "全部":
            seat_name = ONLY_SEAT_TO_NAME.get(self.active_only_seat, "")
            if not seat_name or not has_enough_inventory(get_seat_value(ticket, seat_name), 1):
                return False

        return True

    def _sort_key(self, ticket: dict):
        if self.active_sort == "耗时最短":
            return (duration_to_minutes(ticket.get("duration")), ticket.get("depart_time", ""))
        if self.active_sort == "价格最低":
            return (ticket_lowest_available_price(ticket), duration_to_minutes(ticket.get("duration")), ticket.get("depart_time", ""))
        return (ticket.get("depart_time", ""), duration_to_minutes(ticket.get("duration")))

    def get_filtered_sorted_tickets(self, segment_index: int) -> tuple[list[dict], int]:
        original_tickets = self.segment_tickets.get(segment_index, [])
        filtered = [ticket for ticket in original_tickets if self._matches_filters(ticket)]
        filtered.sort(key=self._sort_key)
        return filtered, len(original_tickets)

    def apply_filters_and_sort(self):
        filtered, total_count = self.get_filtered_sorted_tickets(self.current_segment_index)
        self.render_tickets(self.current_segment_index, filtered, total_count=total_count, from_filter=True)

    def switch_segment(self, index: int):
        if index == self.current_segment_index:
            return
        self.save_query_fields(show_error=False)
        self.current_segment_index = index
        self.refresh_segment_tabs()
        self.load_current_segment_fields()
        self.render_segment_summary()
        tickets = self.segment_tickets.get(index, [])
        if tickets:
            self.apply_filters_and_sort()
        else:
            self.notice_label.setText("请先查询当前段车次。")
            self.result_count_label.setText("共0趟 · 已筛选0趟")
            self.table.setRowCount(0)

    def add_segment(self):
        self.save_query_fields(show_error=False)
        segments = self.app_window.get_segments()
        previous = segments[-1]
        new_segment = build_empty_segment(previous)
        new_segment["from_station"] = previous.get("to_station", "")
        new_segment["from_station_name"] = previous.get("to_station_name", "")
        new_segment["to_station"] = ""
        new_segment["to_station_name"] = ""
        segments.append(new_segment)
        self.current_segment_index = len(segments) - 1
        self.app_window.save_config_data()
        self.refresh_segment_tabs()
        self.load_current_segment_fields()
        self.render_segment_summary()
        self.notice_label.setText("请设置这一段的出发地、目的地和日期，然后点击查询。")
        self.table.setRowCount(0)

    def remove_current_segment(self):
        segments = self.app_window.get_segments()
        if len(segments) <= 1:
            return
        removed_index = self.current_segment_index
        segments.pop(removed_index)
        self.segment_tickets = {
            (index if index < removed_index else index - 1): value
            for index, value in self.segment_tickets.items()
            if index != removed_index
        }
        self.selected_seats = {
            (index if index < removed_index else index - 1): value
            for index, value in self.selected_seats.items()
            if index != removed_index
        }
        self.current_segment_index = max(0, removed_index - 1)
        self.app_window.save_config_data()
        self.refresh_segment_tabs()
        self.load_current_segment_fields()
        self.render_segment_summary()
        tickets = self.segment_tickets.get(self.current_segment_index, [])
        if tickets:
            self.apply_filters_and_sort()
        else:
            self.table.setRowCount(0)
            self.notice_label.setText("请先查询当前段车次。")
            self.result_count_label.setText("共0趟 · 已筛选0趟")

    def save_query_fields(self, show_error: bool = True) -> bool:
        from_station = self.from_input.resolve_station()
        to_station = self.to_input.resolve_station()
        train_date = self.date_input.date().toString("yyyy-MM-dd")
        if not from_station or not to_station:
            if show_error:
                QMessageBox.warning(self, "提示", "请先选择有效的出发地和目的地")
            return False
        segment = self._get_current_segment()
        route_changed = any(
            segment.get(field, "") != value
            for field, value in (
                ("from_station", from_station["code"]),
                ("to_station", to_station["code"]),
                ("train_date", train_date),
            )
        )
        segment["from_station"] = from_station["code"]
        segment["from_station_name"] = from_station["name"]
        segment["to_station"] = to_station["code"]
        segment["to_station_name"] = to_station["name"]
        segment["train_date"] = train_date
        if route_changed:
            segment["primary"] = {}
            segment["backup"] = {}
            self.segment_tickets.pop(self.current_segment_index, None)
            self.selected_seats.pop(self.current_segment_index, None)
        self.app_window.save_config_data()
        self.refresh_segment_tabs()
        self.render_segment_summary()
        self.current_segment_label.setText(f"当前编辑：{format_segment_route(segment, self.current_segment_index)}")
        return True

    def refresh_tickets(self):
        if not self.save_query_fields():
            return
        self.notice_label.setText("正在查询当前段车次和价格，请稍候...")
        self.start_query_feedback()
        segment_index = self.current_segment_index
        self.query_request_id += 1
        self.active_query_request_id = self.query_request_id
        runtime_config = build_segment_runtime_config(self.app_window.config_data, self._get_current_segment())
        self.query_worker = QueryWorker(self.active_query_request_id, segment_index, runtime_config)
        self.query_worker.result_signal.connect(self.handle_query_result)
        self.query_worker.error_signal.connect(self.on_query_error)
        self.query_worker.start()

    def on_query_error(self, request_id: int, message: str):
        if request_id != self.active_query_request_id or self.query_timed_out:
            return
        self.stop_query_feedback()
        self.query_status_label.setText(message)
        self.query_status_label.setStyleSheet("color: #dc2626; font-weight: 700;")
        self.notice_label.setText(message)
        self.result_count_label.setText("共0趟 · 已筛选0趟")
        self.table.setRowCount(0)

    def handle_query_result(self, request_id: int, segment_index: int, tickets: list[dict], is_final: bool):
        if request_id != self.active_query_request_id or self.query_timed_out:
            return
        self.segment_tickets[segment_index] = tickets
        self.apply_filters_and_sort()
        if not is_final:
            self.stop_query_feedback()
            self.query_status_label.setText(f"查询完成，耗时{self.query_elapsed_seconds}秒，共{len(tickets)}趟车次")
            self.query_status_label.setStyleSheet("color: #16a34a; font-weight: 700;")
            return
        self.notice_label.setText(f"价格补全完成，当前段共{len(tickets)}趟车次。")

    def on_seat_changed(self, segment_index: int, ticket_key: str, seat_name: str):
        self.selected_seats.setdefault(segment_index, {})[ticket_key] = seat_name

    def assign_selection(self, slot: str, ticket: dict, seat_name: str):
        selection = {
            "train_code": ticket.get("train_code"),
            "depart_time": ticket.get("depart_time"),
            "arrive_time": ticket.get("arrive_time"),
            "seat_name": seat_name,
        }
        self._get_current_segment()[slot] = selection
        self.app_window.save_config_data()
        self.render_segment_summary()
        self.update_current_selection_status()
        self.apply_filters_and_sort()
        label = "首选" if slot == "primary" else "保底"
        self.notice_label.setText(f"已将 {ticket.get('train_code')} 设置为当前段{label}车票。")

    def get_ticket_role(self, segment: dict, ticket: dict) -> str | None:
        primary = segment.get("primary", {})
        backup = segment.get("backup", {})
        if selection_matches_ticket(primary, ticket):
            return "primary"
        if selection_matches_ticket(backup, ticket):
            return "backup"
        return None

    def apply_row_style(self, row: int, role: str | None):
        if role == "primary":
            bg = QColor("#1a3c8f")
            fg = QColor("#ffffff")
        elif role == "backup":
            bg = QColor("#e8330a")
            fg = QColor("#ffffff")
        else:
            bg = QColor("#ffffff") if row % 2 == 0 else QColor("#f8fafc")
            fg = QColor("#1f2937")

        for column in range(8):
            item = self.table.item(row, column)
            if item is not None:
                item.setBackground(bg)
                item.setForeground(fg)

        for column in (8, 9, 10):
            widget = self.table.cellWidget(row, column)
            if widget is not None:
                if role == "primary":
                    widget.setStyleSheet("background: #1a3c8f; color: #ffffff; border: 1px solid #ffffff; border-radius: 6px;")
                elif role == "backup":
                    widget.setStyleSheet("background: #e8330a; color: #ffffff; border: 1px solid #ffffff; border-radius: 6px;")
                else:
                    widget.setStyleSheet("")

    def render_tickets(
        self,
        segment_index: int,
        tickets: list[dict],
        total_count: int | None = None,
        from_filter: bool = False,
    ):
        if not from_filter:
            self.segment_tickets[segment_index] = tickets
        if segment_index != self.current_segment_index:
            return

        if total_count is None:
            total_count = len(self.segment_tickets.get(segment_index, tickets))
        self.result_count_label.setText(f"共{total_count}趟 · 已筛选{len(tickets)}趟")
        self.notice_label.setText(f"当前段共查询到 {total_count} 趟车次。")
        self.table.setRowCount(len(tickets))
        segment = self._get_current_segment()
        self.update_current_selection_status()
        selected_map = self.selected_seats.setdefault(segment_index, {})

        for row, ticket in enumerate(tickets):
            ticket_key = self._ticket_key(ticket)
            role = self.get_ticket_role(segment, ticket)
            role_prefix = "【首选】" if role == "primary" else "【保底】" if role == "backup" else ""
            self.table.setItem(row, 0, QTableWidgetItem(f"{role_prefix}{ticket.get('train_code', '')}"))
            self.table.setItem(row, 1, QTableWidgetItem(f"{ticket.get('depart_time', '-')} → {ticket.get('arrive_time', '-')}"))
            self.table.setItem(row, 2, QTableWidgetItem(ticket.get("duration", "-")))

            for column, seat_name in zip((3, 4, 5, 6, 7), DISPLAY_SEAT_COLUMNS):
                text, state = get_seat_display_state(ticket, seat_name)
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if role in {"primary", "backup"}:
                    item.setForeground(QColor("#ffffff"))
                elif state == "available":
                    item.setForeground(QColor("#16a34a"))
                elif state == "waitlist":
                    item.setForeground(QColor("#ca8a04"))
                else:
                    item.setForeground(QColor("#94a3b8"))
                self.table.setItem(row, column, item)

            default_seat = selected_map.get(ticket_key)
            if not default_seat:
                for slot in ("primary", "backup"):
                    selection = segment.get(slot, {})
                    if (
                        selection.get("train_code") == ticket.get("train_code")
                        and selection.get("depart_time") == ticket.get("depart_time")
                        and selection.get("arrive_time") == ticket.get("arrive_time")
                    ):
                        default_seat = selection.get("seat_name", "二等座")
                        break
            if not default_seat:
                default_seat = "二等座"
            selected_map[ticket_key] = default_seat

            combo = QComboBox()
            combo.addItems(SEAT_OPTIONS)
            combo.setCurrentText(default_seat)
            combo.currentTextChanged.connect(lambda text, i=segment_index, key=ticket_key: self.on_seat_changed(i, key, text))
            self.table.setCellWidget(row, 8, combo)

            primary_button = QPushButton("设为首选")
            primary_button.setObjectName("secondaryButton")
            primary_button.clicked.connect(lambda _checked=False, t=ticket, c=combo: self.assign_selection("primary", t, c.currentText()))
            self.table.setCellWidget(row, 9, primary_button)

            backup_button = QPushButton("设为保底")
            backup_button.setObjectName("secondaryButton")
            backup_button.clicked.connect(lambda _checked=False, t=ticket, c=combo: self.assign_selection("backup", t, c.currentText()))
            self.table.setCellWidget(row, 10, backup_button)

            self.apply_row_style(row, role)

        self.table.resizeRowsToContents()
        if len(tickets) == 0:
            self.notice_label.setText("当前筛选条件下没有车次，请调整筛选条件后重试。")

    def start_ticketing(self):
        if not self.save_query_fields():
            return
        if not self.app_window.config_data.get("passengers"):
            QMessageBox.warning(self, "提示", "请先在步骤2添加乘客")
            return
        for index, segment in enumerate(self.app_window.get_segments(), start=1):
            if not segment.get("from_station") or not segment.get("to_station") or not segment.get("train_date"):
                QMessageBox.warning(self, "提示", f"第{index}段的出发地、目的地或日期还未设置完整")
                return
            if not segment.get("primary") or not segment.get("backup"):
                QMessageBox.warning(self, "提示", f"请先为第{index}段分别设置首选车票和保底车票")
                return
        self.app_window.show_step(3)
        self.app_window.ticketing_page.start_ticketing()


class TicketingPage(QWidget):
    def __init__(self, app_window):
        super().__init__()
        self.app_window = app_window
        self.worker = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("步骤4 - 抢票")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        status_row = QHBoxLayout()
        self.status_label = QLabel("等待放票中")
        self.status_label.setObjectName("statusPill")
        self.active_target_label = QLabel("当前目标：第1段 首选车票")
        self.active_target_label.setObjectName("activePill")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.active_target_label, 1)
        layout.addLayout(status_row)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, 1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        stop_button = QPushButton("停止")
        stop_button.setObjectName("dangerButton")
        stop_button.clicked.connect(self.stop_ticketing)
        bottom_row.addWidget(stop_button)
        layout.addLayout(bottom_row)

    def on_show(self):
        pass

    def append_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.append(f"[{timestamp}] {message}")

    def set_status(self, text: str, fg: str, bg: str):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 8px; padding: 10px 14px; font-weight: 700;"
        )

    def set_active_target(self, text: str):
        self.active_target_label.setText(text)

    def start_ticketing(self):
        if self.app_window.running:
            return
        self.log_box.clear()
        self.append_log("正在准备抢票任务...")
        self.worker = TicketingWorker(self.app_window.config_data)
        self.worker.log_signal.connect(self.append_log)
        self.worker.status_signal.connect(self.set_status)
        self.worker.active_target_signal.connect(self.set_active_target)
        self.worker.success_signal.connect(self.on_success)
        self.worker.failure_signal.connect(self.on_failure)
        self.worker.finished.connect(self.on_worker_finished)
        self.app_window.running = True
        self.worker.start()

    def stop_ticketing(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.append_log("已手动停止抢票")
        self.app_window.running = False
        self.set_status("已停止", "#475569", "#e2e8f0")

    def on_success(self, message: str):
        self.append_log(message)
        self.set_status("抢票成功", "#166534", "#dcfce7")
        self.app_window.show_success_popup(message)

    def on_failure(self, message: str):
        self.append_log(message)
        if message != "已手动停止抢票":
            self.set_status(message, "#b91c1c", "#fee2e2")

    def on_worker_finished(self):
        self.app_window.running = False


class TicketApp(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_runtime_files()
        self.setWindowTitle("12306 抢票桌面助手")
        self.resize(1000, 680)
        self.setMinimumSize(1000, 680)
        logger.add("runtime.log", rotation="1 MB", encoding="utf-8")

        self.config_data = load_config()
        self._normalize_segments_config()
        self.running = False
        self.nav_buttons = []

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        nav_bar = QFrame()
        nav_bar.setObjectName("navBar")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(14, 10, 14, 10)
        nav_layout.setSpacing(10)
        for index, title in enumerate(STEP_TITLES):
            button = QPushButton(title)
            button.setObjectName("navButton")
            button.clicked.connect(lambda _checked=False, i=index: self.show_step(i))
            self.nav_buttons.append(button)
            nav_layout.addWidget(button)
        nav_layout.addStretch(1)
        root_layout.addWidget(nav_bar)

        self.stack = QStackedWidget()
        root_layout.addWidget(self.stack, 1)

        self.cookie_page = CookiePage(self)
        self.passenger_page = PassengerPage(self)
        self.selection_page = TicketSelectionPage(self)
        self.ticketing_page = TicketingPage(self)
        self.pages = [self.cookie_page, self.passenger_page, self.selection_page, self.ticketing_page]
        for page in self.pages:
            self.stack.addWidget(page)

        self.setStyleSheet(self._build_stylesheet())
        self.show_step(0)

    def _build_stylesheet(self) -> str:
        return """
        QMainWindow, QWidget {
            background: #f5f7fb;
            color: #1f2937;
            font-family: "Microsoft YaHei UI";
            font-size: 13px;
        }
        QFrame#navBar {
            background: #1a3c8f;
        }
        QPushButton#navButton {
            background: rgba(255, 255, 255, 0.16);
            color: #ffffff;
            border: none;
            border-radius: 8px;
            padding: 10px 16px;
            font-weight: 700;
        }
        QPushButton#navButton[active="true"] {
            background: #ffffff;
            color: #1a3c8f;
        }
        QTabWidget::pane {
            border: 1px solid #d8e3ff;
            border-radius: 10px;
            background: #ffffff;
            top: -1px;
        }
        QTabBar::tab {
            background: #e5e7eb;
            color: #111827;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            padding: 10px 18px;
            margin-right: 6px;
            font-weight: 700;
        }
        QTabBar::tab:selected {
            background: #1a3c8f;
            color: #ffffff;
        }
        QLabel#pageTitle {
            font-size: 22px;
            font-weight: 700;
            color: #1f2937;
        }
        QLabel#pageHint, QLabel#statusText {
            color: #64748b;
        }
        QLabel#fieldLabel {
            color: #334155;
            font-weight: 600;
        }
        QPushButton#primaryButton {
            background: #e8330a;
            color: #ffffff;
            border: none;
            border-radius: 8px;
            padding: 10px 18px;
            font-weight: 700;
        }
        QPushButton#primaryButton:hover {
            background: #cf2d08;
        }
        QPushButton#secondaryButton {
            background: #ffffff;
            color: #1a3c8f;
            border: 1px solid #bcd0ff;
            border-radius: 8px;
            padding: 8px 14px;
            font-weight: 600;
        }
        QPushButton#dangerButton {
            background: #ef4444;
            color: #ffffff;
            border: none;
            border-radius: 8px;
            padding: 8px 14px;
            font-weight: 700;
        }
        QPushButton#segmentTab {
            background: #ffffff;
            color: #1f2937;
            border: 1px solid #d8e3ff;
            border-radius: 8px;
            padding: 10px 14px;
            font-weight: 600;
        }
        QPushButton#filterChip, QPushButton#sortChip {
            background: #e5e7eb;
            color: #111827;
            border: none;
            border-radius: 8px;
            padding: 8px 12px;
            font-weight: 600;
        }
        QPushButton#filterChip:checked, QPushButton#sortChip:checked {
            background: #1a3c8f;
            color: #ffffff;
        }
        QPushButton#segmentTab[active="true"] {
            background: #1a3c8f;
            color: #ffffff;
            border: none;
        }
        QLabel#statusPill {
            background: #fff7ed;
            color: #c2410c;
            border-radius: 8px;
            padding: 10px 14px;
            font-weight: 700;
        }
        QLabel#activePill {
            background: #dbeafe;
            color: #1d4ed8;
            border-radius: 8px;
            padding: 10px 14px;
            font-weight: 700;
        }
        QLabel#selectionStatusPrimary {
            background: #dbeafe;
            color: #1a3c8f;
            border-radius: 8px;
            padding: 10px 14px;
            font-weight: 700;
        }
        QLabel#selectionStatusBackup {
            background: #fff1eb;
            color: #e8330a;
            border-radius: 8px;
            padding: 10px 14px;
            font-weight: 700;
        }
        QFrame#summaryCard {
            background: #ffffff;
            border: 1px solid #d8e3ff;
            border-radius: 10px;
        }
        QLabel#cardTitle {
            font-weight: 700;
            color: #1f2937;
        }
        QLineEdit, QPlainTextEdit, QTextEdit, QDateEdit, QComboBox, QTableWidget {
            background: #ffffff;
            border: 1px solid #d6dce8;
            border-radius: 8px;
            padding: 6px 8px;
        }
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QDateEdit:focus, QComboBox:focus {
            border: 1px solid #1a3c8f;
        }
        QTableWidget {
            gridline-color: #e2e8f0;
            alternate-background-color: #f8fafc;
            selection-background-color: #e0ecff;
        }
        QHeaderView::section {
            background: #edf3ff;
            color: #1a3c8f;
            font-weight: 700;
            border: none;
            padding: 8px;
        }
        """

    def _normalize_segments_config(self):
        query_config = self.config_data.setdefault("query", {})
        segments = self.config_data.get("segments")
        if isinstance(segments, list) and segments:
            normalized_segments = [
                normalize_segment(segment, query_config if index == 0 else {})
                for index, segment in enumerate(segments)
            ]
        else:
            legacy_selection = self.config_data.get("selection", {})
            first_segment = normalize_segment(
                {
                    **query_config,
                    "primary": legacy_selection.get("primary", {}),
                    "backup": legacy_selection.get("backup", {}),
                },
                query_config,
            )
            normalized_segments = [first_segment]
        self.config_data["segments"] = normalized_segments
        self._sync_legacy_selection()

    def _sync_legacy_selection(self):
        segments = self.get_segments()
        first_segment = segments[0]
        query_config = self.config_data.setdefault("query", {})
        for field in SEGMENT_QUERY_FIELDS:
            query_config[field] = first_segment.get(field, "")
        query_config["passenger_count"] = max(1, len(self.config_data.get("passengers", [])))
        self.config_data["selection"] = {
            "primary": copy.deepcopy(first_segment.get("primary", {})),
            "backup": copy.deepcopy(first_segment.get("backup", {})),
        }

    def get_segments(self) -> list[dict]:
        segments = self.config_data.setdefault("segments", [])
        if not segments:
            segments.append(build_empty_segment(self.config_data.get("query", {})))
        return segments

    def save_config_data(self):
        self._sync_legacy_selection()
        save_config(self.config_data)

    def show_step(self, index: int):
        self.stack.setCurrentIndex(index)
        for current_index, button in enumerate(self.nav_buttons):
            button.setProperty("active", current_index == index)
            button.style().unpolish(button)
            button.style().polish(button)
        page = self.pages[index]
        if hasattr(page, "on_show"):
            page.on_show()

    def mask_id_card(self, id_card: str) -> str:
        id_card = (id_card or "").strip()
        if len(id_card) <= 4:
            return "*" * len(id_card)
        return "*" * (len(id_card) - 4) + id_card[-4:]

    def show_success_popup(self, message: str):
        box = QMessageBox(self)
        box.setWindowTitle("抢票成功")
        box.setText(message)
        box.setIcon(QMessageBox.Information)
        box.setStyleSheet(
            "QLabel { min-width: 320px; font-size: 14px; }"
            "QPushButton { background: #10b981; color: white; border: none; border-radius: 6px; padding: 8px 16px; }"
        )
        box.exec_()


def main():
    application = QApplication(sys.argv)
    window = TicketApp()
    window.show()
    sys.exit(application.exec_())


if __name__ == "__main__":
    main()
