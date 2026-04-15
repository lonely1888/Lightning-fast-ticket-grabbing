import re
import time
from datetime import datetime
from typing import Callable
from urllib.parse import unquote

import requests
from loguru import logger

from login import login
from query import get_preferred_seat, get_seat_value, has_enough_inventory


CHECK_USER_URL = "https://kyfw.12306.cn/otn/login/checkUser"
SUBMIT_ORDER_URL = "https://kyfw.12306.cn/otn/leftTicket/submitOrderRequest"
INIT_DC_URL = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
PASSENGER_DTOS_URL = "https://kyfw.12306.cn/otn/confirmPassenger/getPassengerDTOs"
CHECK_ORDER_INFO_URL = "https://kyfw.12306.cn/otn/confirmPassenger/checkOrderInfo"
GET_QUEUE_COUNT_URL = "https://kyfw.12306.cn/otn/confirmPassenger/getQueueCount"
CONFIRM_SINGLE_URL = "https://kyfw.12306.cn/otn/confirmPassenger/confirmSingleForQueue"
QUERY_WAIT_TIME_URL = "https://kyfw.12306.cn/otn/confirmPassenger/queryOrderWaitTime"

SEAT_NAME_TO_CODE = {
    "二等座": "O",
    "一等座": "M",
    "商务座": "9",
    "硬卧": "3",
    "硬座": "1",
}


def _emit(message: str, log_callback: Callable[[str], None] | None = None, level: str = "info") -> None:
    getattr(logger, level)(message)
    if log_callback is not None:
        log_callback(message)


def _result(success: bool, message: str, retryable: bool = True, **extra) -> dict:
    payload = {"success": success, "message": message, "retryable": retryable}
    payload.update(extra)
    return payload


def _fail(step: str, message: str, retryable: bool, log_callback: Callable[[str], None] | None) -> dict:
    full_message = f"{step}失败: {message}"
    _emit(full_message, log_callback=log_callback, level="error")
    return _result(False, full_message, retryable=retryable)


def _load_passenger_config(config: dict) -> list[dict]:
    passengers = config.get("passengers", [])
    if not isinstance(passengers, list):
        return []

    cleaned = []
    for passenger in passengers:
        if not isinstance(passenger, dict):
            continue
        cleaned.append(
            {
                "name": str(passenger.get("name", "")).strip(),
                "id_card": str(passenger.get("id_card", passenger.get("id_no", ""))).strip(),
            }
        )
    return cleaned


def _extract_first(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return match.group(1)
    return ""


def _format_train_date(train_date: str) -> str:
    parsed = datetime.strptime(train_date, "%Y-%m-%d")
    return parsed.strftime("%a %b %d %Y 00:00:00 GMT+0800 (China Standard Time)")


def _check_user(session: requests.Session) -> tuple[bool, str]:
    try:
        response = session.post(CHECK_USER_URL, data={"_json_att": ""}, timeout=15)
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError) as exc:
        return False, str(exc)

    if not response_json.get("status", False):
        return False, "12306 未返回有效的登录校验结果"

    if not response_json.get("data", {}).get("flag", False):
        return False, "当前 Cookie 未通过下单校验，可能只能查票不能购票"

    return True, ""


def _submit_order_request(session: requests.Session, ticket: dict, train_date: str) -> tuple[bool, str]:
    payload = {
        "secretStr": unquote(ticket.get("secret_str", "")),
        "train_date": train_date,
        "back_train_date": train_date,
        "tour_flag": "dc",
        "purpose_codes": "ADULT",
        "query_from_station_name": ticket.get("from_station_name", ticket.get("from_station_code", "")),
        "query_to_station_name": ticket.get("to_station_name", ticket.get("to_station_code", "")),
        "bed_level_info": "0",
        "seatDiscountInfo": "",
        "_json_att": "",
    }

    try:
        response = session.post(SUBMIT_ORDER_URL, data=payload, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        return False, str(exc)

    if "/otn/passport" in response.url:
        return False, "12306 将请求重定向到登录页，当前登录态不足以下单"

    if "系统忙" in response.text:
        return False, "12306 返回系统忙，请稍后重试"

    return True, ""


def _init_dc(session: requests.Session) -> tuple[dict, str]:
    try:
        response = session.post(INIT_DC_URL, data={"_json_att": ""}, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        return {}, str(exc)

    if "/otn/passport" in response.url:
        return {}, "12306 将 initDc 重定向到登录页，当前登录态不足以下单"

    html = response.text
    init_data = {
        "repeat_submit_token": _extract_first(
            html,
            [
                r"globalRepeatSubmitToken\s*=\s*'([^']+)'",
                r'globalRepeatSubmitToken\s*=\s*"([^"]+)"',
            ],
        ),
        "key_check_isChange": _extract_first(
            html,
            [
                r"'key_check_isChange':'([^']+)'",
                r'"key_check_isChange":"([^"]+)"',
            ],
        ),
        "left_ticket_str": _extract_first(
            html,
            [
                r"'leftTicketStr':'([^']+)'",
                r'"leftTicketStr":"([^"]+)"',
            ],
        ),
        "train_location": _extract_first(
            html,
            [
                r"'train_location':'([^']+)'",
                r'"train_location":"([^"]+)"',
            ],
        ),
        "purpose_codes": _extract_first(
            html,
            [
                r"'purpose_codes':'([^']+)'",
                r'"purpose_codes":"([^"]+)"',
            ],
        )
        or "00",
    }

    required_keys = ("repeat_submit_token", "key_check_isChange", "left_ticket_str", "train_location")
    missing = [key for key in required_keys if not init_data.get(key)]
    if missing:
        return {}, f"缺少关键字段: {', '.join(missing)}"

    return init_data, ""


def _fetch_passenger_dtos(session: requests.Session, repeat_submit_token: str) -> tuple[list[dict], str]:
    payload = {"_json_att": "", "REPEAT_SUBMIT_TOKEN": repeat_submit_token}

    try:
        response = session.post(PASSENGER_DTOS_URL, data=payload, timeout=15)
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError) as exc:
        return [], str(exc)

    if not response_json.get("status", False):
        return [], "12306 未返回有效乘客列表"

    passenger_list = response_json.get("data", {}).get("normal_passengers", [])
    if not isinstance(passenger_list, list):
        return [], "乘客列表格式异常"

    return passenger_list, ""


def _match_passengers(config_passengers: list[dict], available_passengers: list[dict]) -> tuple[list[dict], str]:
    passenger_index = {}
    for passenger in available_passengers:
        name = str(passenger.get("passenger_name", "")).strip()
        id_card = str(passenger.get("passenger_id_no", "")).strip()
        if name:
            passenger_index[f"name:{name}"] = passenger
        if id_card:
            passenger_index[f"id:{id_card}"] = passenger

    matched = []
    for passenger in config_passengers:
        name = passenger.get("name", "")
        id_card = passenger.get("id_card", "")
        found = None

        if id_card:
            found = passenger_index.get(f"id:{id_card}")
        if found is None and name:
            found = passenger_index.get(f"name:{name}")

        if found is None:
            return [], f"未在 12306 乘车人列表中找到乘客: {name or '<未命名乘客>'}"

        matched.append(found)

    return matched, ""


def _build_passenger_ticket_str(passengers: list[dict], seat_code: str) -> str:
    rows = []
    for passenger in passengers:
        rows.append(
            ",".join(
                [
                    seat_code,
                    "0",
                    str(passenger.get("passenger_type", "1")),
                    str(passenger.get("passenger_name", "")).strip(),
                    str(passenger.get("passenger_id_type_code", "1")).strip(),
                    str(passenger.get("passenger_id_no", "")).strip(),
                    str(passenger.get("mobile_no", "")).strip(),
                    "N",
                    str(passenger.get("allEncStr", "")).strip(),
                ]
            )
        )
    return "_".join(rows)


def _build_old_passenger_str(passengers: list[dict]) -> str:
    rows = []
    for passenger in passengers:
        rows.append(
            ",".join(
                [
                    str(passenger.get("passenger_name", "")).strip(),
                    str(passenger.get("passenger_id_type_code", "1")).strip(),
                    str(passenger.get("passenger_id_no", "")).strip(),
                    str(passenger.get("passenger_type", "1")).strip(),
                ]
            )
        )
    return "_".join(rows) + "_"


def _check_order_info(
    session: requests.Session,
    repeat_submit_token: str,
    passenger_ticket_str: str,
    old_passenger_str: str,
) -> tuple[bool, str]:
    payload = {
        "cancel_flag": "2",
        "bed_level_order_num": "000000000000000000000000000000",
        "passengerTicketStr": passenger_ticket_str,
        "oldPassengerStr": old_passenger_str,
        "tour_flag": "dc",
        "randCode": "",
        "whatsSelect": "1",
        "_json_att": "",
        "REPEAT_SUBMIT_TOKEN": repeat_submit_token,
    }

    try:
        response = session.post(CHECK_ORDER_INFO_URL, data=payload, timeout=15)
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError) as exc:
        return False, str(exc)

    if not response_json.get("status", False):
        return False, "12306 未返回有效 checkOrderInfo 结果"

    submit_status = response_json.get("data", {}).get("submitStatus")
    if submit_status is False:
        messages = response_json.get("data", {}).get("errMsg") or response_json.get("messages") or []
        if isinstance(messages, list):
            message = " / ".join(str(item) for item in messages if item)
        else:
            message = str(messages)
        return False, message or "12306 拒绝提交订单信息"

    return True, ""


def _get_queue_count(
    session: requests.Session,
    repeat_submit_token: str,
    ticket: dict,
    seat_code: str,
    init_data: dict,
    train_date: str,
) -> tuple[bool, str]:
    payload = {
        "train_date": _format_train_date(train_date),
        "train_no": ticket.get("train_no", ""),
        "stationTrainCode": ticket.get("train_code", ""),
        "seatType": seat_code,
        "fromStationTelecode": ticket.get("from_station_code", ""),
        "toStationTelecode": ticket.get("to_station_code", ""),
        "leftTicket": init_data.get("left_ticket_str", ""),
        "purpose_codes": init_data.get("purpose_codes", "00"),
        "train_location": init_data.get("train_location", ticket.get("train_location", "")),
        "_json_att": "",
        "REPEAT_SUBMIT_TOKEN": repeat_submit_token,
    }

    try:
        response = session.post(GET_QUEUE_COUNT_URL, data=payload, timeout=15)
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError) as exc:
        return False, str(exc)

    if not response_json.get("status", False):
        return False, "12306 未返回有效排队信息"

    return True, ""


def _confirm_single_for_queue(
    session: requests.Session,
    repeat_submit_token: str,
    init_data: dict,
    passenger_ticket_str: str,
    old_passenger_str: str,
) -> tuple[bool, str]:
    payload = {
        "passengerTicketStr": passenger_ticket_str,
        "oldPassengerStr": old_passenger_str,
        "randCode": "",
        "purpose_codes": init_data.get("purpose_codes", "00"),
        "key_check_isChange": init_data.get("key_check_isChange", ""),
        "leftTicketStr": init_data.get("left_ticket_str", ""),
        "train_location": init_data.get("train_location", ""),
        "choose_seats": "",
        "seatDetailType": "000",
        "whatsSelect": "1",
        "roomType": "00",
        "dwAll": "N",
        "_json_att": "",
        "REPEAT_SUBMIT_TOKEN": repeat_submit_token,
    }

    try:
        response = session.post(CONFIRM_SINGLE_URL, data=payload, timeout=15)
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError) as exc:
        return False, str(exc)

    if not response_json.get("status", False):
        return False, "12306 未返回有效 confirmSingleForQueue 结果"

    if response_json.get("data", {}).get("submitStatus") is False:
        return False, "12306 未接受最终排队请求"

    return True, ""


def _wait_order_result(
    session: requests.Session,
    repeat_submit_token: str,
    log_callback: Callable[[str], None] | None,
) -> tuple[bool, str]:
    for _ in range(15):
        params = {
            "random": int(time.time() * 1000),
            "tourFlag": "dc",
            "_json_att": "",
            "REPEAT_SUBMIT_TOKEN": repeat_submit_token,
        }

        try:
            response = session.get(QUERY_WAIT_TIME_URL, params=params, timeout=15)
            response.raise_for_status()
            response_json = response.json()
        except (requests.RequestException, ValueError) as exc:
            return False, str(exc)

        if not response_json.get("status", False):
            return False, "12306 未返回有效排队结果"

        data = response_json.get("data", {})
        order_id = data.get("orderId")
        if order_id:
            return True, str(order_id)

        wait_time = data.get("waitTime")
        _emit(f"12306 排队中，waitTime={wait_time}", log_callback=log_callback)
        time.sleep(2)

    return False, "排队超时，暂未获取到订单号"


def place_order(
    session: requests.Session,
    config: dict,
    ticket: dict,
    seat_name: str | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    query_config = config.get("query", {})
    train_date = str(query_config.get("train_date", "")).strip()
    passenger_count = int(query_config.get("passenger_count", 1))
    seat_preference = query_config.get("seat_preference", [])

    cookie_dict = login(session, verbose=False)
    if not cookie_dict:
        return _fail("登录校验", "Cookie 已过期，请重新手动登录", False, log_callback)

    config_passengers = _load_passenger_config(config)
    if len(config_passengers) < passenger_count:
        return _fail("乘客信息校验", "配置中的乘客数量不足", False, log_callback)

    selected_passengers = config_passengers[:passenger_count]
    if any(not item.get("name") or not item.get("id_card") for item in selected_passengers):
        return _fail("乘客信息校验", "乘客姓名或身份证号为空", False, log_callback)

    chosen_seat_name = seat_name or get_preferred_seat(ticket, seat_preference, passenger_count)
    if not chosen_seat_name:
        return _fail("席别匹配", "目标车次余票不足，未命中席别优先级", True, log_callback)

    current_seat_value = get_seat_value(ticket, chosen_seat_name)
    if not has_enough_inventory(current_seat_value, passenger_count):
        return _fail("席别匹配", f"{chosen_seat_name} 余票不足 {passenger_count} 张", True, log_callback)

    seat_code = SEAT_NAME_TO_CODE.get(chosen_seat_name)
    if not seat_code:
        return _fail("席别转换", f"不支持的席别: {chosen_seat_name}", False, log_callback)

    _emit(
        (
            f"准备下单 train_code={ticket.get('train_code')} "
            f"depart={ticket.get('depart_time')} "
            f"arrive={ticket.get('arrive_time')} "
            f"seat={chosen_seat_name} passengers={passenger_count}"
        ),
        log_callback=log_callback,
    )

    ok, message = _check_user(session)
    if not ok:
        return _fail("checkUser", message, False, log_callback)

    _emit("步骤1：提交预订请求 submitOrderRequest", log_callback=log_callback)
    ok, message = _submit_order_request(session, ticket, train_date)
    if not ok:
        return _fail("submitOrderRequest", message, True, log_callback)

    _emit("步骤2：初始化订单页 initDc", log_callback=log_callback)
    init_data, message = _init_dc(session)
    if not init_data:
        return _fail("initDc", message, True, log_callback)

    repeat_submit_token = init_data["repeat_submit_token"]

    _emit("步骤3：获取乘客信息并校验订单", log_callback=log_callback)
    available_passengers, message = _fetch_passenger_dtos(session, repeat_submit_token)
    if not available_passengers:
        return _fail("getPassengerDTOs", message, False, log_callback)

    matched_passengers, message = _match_passengers(selected_passengers, available_passengers)
    if not matched_passengers:
        return _fail("乘客匹配", message, False, log_callback)

    passenger_ticket_str = _build_passenger_ticket_str(matched_passengers, seat_code)
    old_passenger_str = _build_old_passenger_str(matched_passengers)

    ok, message = _check_order_info(session, repeat_submit_token, passenger_ticket_str, old_passenger_str)
    if not ok:
        return _fail("checkOrderInfo", message, True, log_callback)

    ok, message = _get_queue_count(
        session,
        repeat_submit_token,
        ticket,
        seat_code,
        init_data,
        train_date,
    )
    if not ok:
        return _fail("getQueueCount", message, True, log_callback)

    _emit("步骤4：提交排队 confirmSingleForQueue", log_callback=log_callback)
    ok, message = _confirm_single_for_queue(
        session,
        repeat_submit_token,
        init_data,
        passenger_ticket_str,
        old_passenger_str,
    )
    if not ok:
        return _fail("confirmSingleForQueue", message, True, log_callback)

    ok, message = _wait_order_result(session, repeat_submit_token, log_callback)
    if not ok:
        return _fail("queryOrderWaitTime", message, True, log_callback)

    success_message = "🎉 抢票成功！请30分钟内支付"
    _emit(f"订单已生成，orderId={message}", log_callback=log_callback, level="success")
    return _result(True, success_message, retryable=False, order_id=message)
