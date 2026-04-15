import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import calendar
import requests
import yaml

from login import DEFAULT_USER_AGENT, create_session, login
from paths import get_resource_file, get_runtime_file


CONFIG_PATH = get_runtime_file("config.yaml")
STATION_JS_PATH = get_resource_file("station_name.js")
LEFT_TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/query"
PRICE_URL = "https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice"

SECRET_STR_INDEX = 0
TRAIN_NO_INDEX = 2
TRAIN_CODE_INDEX = 3
FROM_STATION_CODE_INDEX = 6
TO_STATION_CODE_INDEX = 7
START_TIME_INDEX = 8
ARRIVE_TIME_INDEX = 9
DURATION_INDEX = 10
CAN_WEB_BUY_INDEX = 11
TRAIN_DATE_INDEX = 13
TRAIN_LOCATION_INDEX = 15
FROM_STATION_NO_INDEX = 16
TO_STATION_NO_INDEX = 17
SOFT_SLEEPER_INDEX = 23
NO_SEAT_INDEX = 26
HARD_SLEEPER_INDEX = 28
HARD_SEAT_INDEX = 29
SECOND_CLASS_INDEX = 30
FIRST_CLASS_INDEX = 31
BUSINESS_CLASS_INDEX = 32
SEAT_TYPES_INDEX = 35

SEAT_KEY_TO_LABEL = {
    "second_class": "二等座",
    "first_class": "一等座",
    "business_class": "商务座",
    "hard_sleeper": "硬卧",
    "hard_seat": "硬座",
    "soft_sleeper": "软卧",
    "no_seat": "无座",
}
SEAT_LABEL_TO_KEY = {value: key for key, value in SEAT_KEY_TO_LABEL.items()}

DEFAULT_CONFIG = {
    "query": {
        "from_station": "CSQ",
        "from_station_name": "长沙",
        "to_station": "PXG",
        "to_station_name": "萍乡",
        "train_date": datetime.now().strftime("%Y-%m-%d"),
        "target_depart_time": "21:04",
        "passenger_count": 1,
        "seat_preference": ["二等座", "一等座"],
    },
    "passengers": [],
    "segments": [
        {
            "from_station": "CSQ",
            "from_station_name": "长沙",
            "to_station": "PXG",
            "to_station_name": "萍乡",
            "train_date": datetime.now().strftime("%Y-%m-%d"),
            "primary": {},
            "backup": {},
        }
    ],
    "selection": {"primary": {}, "backup": {}},
    "schedule": {"enabled": True, "interval_seconds": 4},
}

QUERY_CACHE_TTL = 60
_QUERY_CACHE: dict[tuple[str, str, str, bool], tuple[float, list[dict[str, str]]]] = {}
_QUERY_CACHE_LOCK = threading.Lock()
_PRICE_CACHE: dict[tuple[str, str, str, str, str], tuple[float, dict[str, str]]] = {}
_PRICE_CACHE_LOCK = threading.Lock()

POPULAR_STATION_NAMES = [
    "北京", "北京南", "北京西", "上海", "上海虹桥", "上海南", "广州", "广州南", "广州东", "深圳",
    "深圳北", "福田", "天津", "天津西", "重庆", "重庆北", "重庆西", "成都", "成都东", "成都南",
    "贵阳", "贵阳北", "昆明", "昆明南", "长沙", "长沙南", "萍乡", "萍乡北", "南昌", "南昌西",
    "武汉", "汉口", "郑州", "郑州东", "西安", "西安北", "兰州", "兰州西", "西宁", "银川",
    "乌鲁木齐", "拉萨", "呼和浩特", "包头", "太原", "石家庄", "唐山", "秦皇岛", "济南", "济南西",
    "青岛", "青岛北", "烟台", "潍坊", "临沂", "南京", "南京南", "苏州", "无锡", "常州",
    "徐州", "杭州", "杭州东", "宁波", "温州", "金华", "义乌", "合肥", "合肥南", "芜湖",
    "蚌埠", "福州", "福州南", "厦门", "厦门北", "泉州", "漳州", "龙岩", "长春", "吉林",
    "沈阳", "沈阳北", "大连", "哈尔滨", "哈尔滨西", "齐齐哈尔", "佳木斯", "南宁", "桂林", "柳州",
    "北海", "海口", "三亚", "珠海", "东莞", "昆山南", "常熟", "洛阳", "开封", "宝鸡",
]
POPULAR_STATION_PRIORITY = {name: index for index, name in enumerate(POPULAR_STATION_NAMES)}


def _deep_merge(base: dict, incoming: dict) -> dict:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_file: str | Path = CONFIG_PATH) -> dict:
    path = Path(config_file)
    if not path.exists():
        return _deep_merge({}, DEFAULT_CONFIG)

    with path.open("r", encoding="utf-8") as file:
        current = yaml.safe_load(file) or {}

    return _deep_merge(DEFAULT_CONFIG, current)


def save_config(config: dict, config_file: str | Path = CONFIG_PATH) -> None:
    normalized = _deep_merge(DEFAULT_CONFIG, config)
    Path(config_file).write_text(
        yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def normalize_seat_value(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def has_enough_inventory(value: str | None, passenger_count: int) -> bool:
    normalized = normalize_seat_value(value)
    if not normalized or normalized in {"无", "--", "*", "候补"}:
        return False
    if normalized == "有":
        return True
    if normalized.isdigit():
        return int(normalized) >= passenger_count
    return False


def get_preferred_seat(train_info: dict[str, str], seat_preference: list[str], passenger_count: int) -> str | None:
    for seat_name in seat_preference:
        seat_key = SEAT_LABEL_TO_KEY.get(seat_name)
        if not seat_key:
            continue
        if has_enough_inventory(train_info.get(seat_key, ""), passenger_count):
            return seat_name
    return None


def get_seat_value(train_info: dict[str, str], seat_name: str) -> str:
    seat_key = SEAT_LABEL_TO_KEY.get(seat_name, "")
    if not seat_key:
        return ""
    return normalize_seat_value(train_info.get(seat_key, ""))


@lru_cache(maxsize=1)
def load_station_catalog() -> list[dict[str, str]]:
    if not STATION_JS_PATH.exists():
        return []

    text = STATION_JS_PATH.read_text(encoding="utf-8")
    raw = text.split("'", 1)[1].rsplit("'", 1)[0]
    stations: list[dict[str, str]] = []

    for item in raw.split("@"):
        if not item:
            continue
        parts = item.split("|")
        if len(parts) < 5:
            continue

        short_name, name, code, full_pinyin, abbr = parts[:5]
        stations.append(
            {
                "short_name": short_name,
                "name": name,
                "code": code,
                "full_pinyin": full_pinyin,
                "abbr": abbr,
            }
        )

    return stations


def search_stations(keyword: str, limit: int = 8) -> list[dict[str, str]]:
    keyword = keyword.strip().lower()
    if not keyword:
        return load_station_catalog()[:limit]

    matches: list[tuple[int, int, int, dict[str, str]]] = []
    seen_codes: set[str] = set()

    for station in load_station_catalog():
        name = station["name"]
        full_pinyin = station["full_pinyin"].lower()
        abbr = station["abbr"].lower()
        code = station["code"]

        score = None
        if name == keyword:
            score = 0
        elif abbr.startswith(keyword):
            score = 1
        elif full_pinyin.startswith(keyword):
            score = 2
        elif keyword in name:
            score = 3
        elif keyword in abbr:
            score = 4
        elif keyword in full_pinyin:
            score = 5

        if score is None or code in seen_codes:
            continue

        priority = POPULAR_STATION_PRIORITY.get(name, 9999)
        matches.append((score, priority, len(name), station))
        seen_codes.add(code)

    matches.sort(key=lambda item: (item[0], item[1], item[2], item[3]["name"]))
    return [item[3] for item in matches[:limit]]


def find_station(keyword: str) -> dict[str, str] | None:
    keyword = keyword.strip().lower()
    if not keyword:
        return None

    for station in load_station_catalog():
        if (
            station["name"] == keyword
            or station["code"].lower() == keyword
            or station["abbr"].lower() == keyword
            or station["full_pinyin"].lower() == keyword
        ):
            return station

    results = search_stations(keyword, limit=1)
    return results[0] if results else None


def _parse_train_result(raw_result: str, station_map: dict[str, str]) -> dict[str, str]:
    fields = raw_result.split("|")

    def get_field(index: int) -> str:
        if index >= len(fields):
            return ""
        return normalize_seat_value(fields[index])

    from_station_code = get_field(FROM_STATION_CODE_INDEX)
    to_station_code = get_field(TO_STATION_CODE_INDEX)

    return {
        "secret_str": get_field(SECRET_STR_INDEX),
        "train_no": get_field(TRAIN_NO_INDEX),
        "train_code": get_field(TRAIN_CODE_INDEX),
        "from_station_code": from_station_code,
        "to_station_code": to_station_code,
        "from_station_name": station_map.get(from_station_code, from_station_code),
        "to_station_name": station_map.get(to_station_code, to_station_code),
        "depart_time": get_field(START_TIME_INDEX),
        "arrive_time": get_field(ARRIVE_TIME_INDEX),
        "duration": get_field(DURATION_INDEX),
        "can_web_buy": get_field(CAN_WEB_BUY_INDEX),
        "train_date_raw": get_field(TRAIN_DATE_INDEX),
        "train_location": get_field(TRAIN_LOCATION_INDEX),
        "from_station_no": get_field(FROM_STATION_NO_INDEX),
        "to_station_no": get_field(TO_STATION_NO_INDEX),
        "seat_types": get_field(SEAT_TYPES_INDEX),
        "second_class": get_field(SECOND_CLASS_INDEX),
        "first_class": get_field(FIRST_CLASS_INDEX),
        "business_class": get_field(BUSINESS_CLASS_INDEX),
        "hard_sleeper": get_field(HARD_SLEEPER_INDEX),
        "hard_seat": get_field(HARD_SEAT_INDEX),
        "soft_sleeper": get_field(SOFT_SLEEPER_INDEX),
        "no_seat": get_field(NO_SEAT_INDEX),
        "prices": {},
    }


def _normalize_price_value(value: str | None) -> str:
    if value is None:
        return "-"

    normalized = str(value).strip().replace("\xa5", "¥")
    if not normalized:
        return "-"
    if normalized.isdigit():
        return f"¥{int(normalized) / 10:.1f}"
    return normalized


def _fetch_price_for_train(
    cookie_dict: dict[str, str],
    user_agent: str,
    train: dict[str, str],
    train_date: str,
) -> dict[str, str]:
    price_cache_key = (
        train.get("train_no", ""),
        train.get("from_station_no", ""),
        train.get("to_station_no", ""),
        train.get("seat_types", ""),
        train_date,
    )
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(price_cache_key)
        if cached and (time.time() - cached[0] < QUERY_CACHE_TTL):
            return copy.deepcopy(cached[1])

    params = {
        "train_no": train.get("train_no", ""),
        "from_station_no": train.get("from_station_no", ""),
        "to_station_no": train.get("to_station_no", ""),
        "seat_types": train.get("seat_types", ""),
        "train_date": train_date,
    }

    try:
        response = requests.get(
            PRICE_URL,
            params=params,
            headers={
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc",
            },
            cookies=cookie_dict,
            timeout=10,
        )
        response.raise_for_status()
        response_json = response.json()
    except (requests.RequestException, ValueError):
        return {}

    if not response_json.get("status", False):
        return {}

    data = response_json.get("data", {})
    prices = {
        "business_class_price": _normalize_price_value(data.get("A9") or data.get("9")),
        "first_class_price": _normalize_price_value(data.get("M")),
        "second_class_price": _normalize_price_value(data.get("O")),
        "hard_sleeper_price": _normalize_price_value(data.get("A3") or data.get("3")),
        "soft_sleeper_price": _normalize_price_value(data.get("A4") or data.get("4")),
        "hard_seat_price": _normalize_price_value(data.get("A1") or data.get("1")),
        "no_seat_price": _normalize_price_value(data.get("WZ")),
    }
    with _PRICE_CACHE_LOCK:
        _PRICE_CACHE[price_cache_key] = (time.time(), copy.deepcopy(prices))
    return prices


def enrich_ticket_prices(session: requests.Session, trains: list[dict[str, str]], train_date: str) -> None:
    if not trains:
        return

    cookie_dict = session.cookies.get_dict()
    user_agent = session.headers.get("User-Agent", DEFAULT_USER_AGENT)

    with ThreadPoolExecutor(max_workers=min(20, max(4, len(trains)))) as executor:
        future_map = {
            executor.submit(_fetch_price_for_train, cookie_dict, user_agent, train, train_date): train
            for train in trains
        }

        for future in as_completed(future_map):
            train = future_map[future]
            try:
                prices = future.result()
            except Exception:
                prices = {}

            train["prices"] = prices
            train.update(prices)


def _print_train_list(trains: list[dict[str, str]]) -> None:
    if not trains:
        print("当前没有查到可展示的车次", flush=True)
        return

    print("车次查询结果：", flush=True)
    for train in trains:
        print(
            f"{train['train_code']} "
            f"{train['depart_time']}->{train['arrive_time']} "
            f"二等座:{train['second_class'] or '-'} "
            f"一等座:{train['first_class'] or '-'} "
            f"商务座:{train['business_class'] or '-'}",
            flush=True,
        )


def query_tickets(
    session: requests.Session | None = None,
    config: dict | None = None,
    verbose: bool = True,
    include_prices: bool = False,
    debug: bool = False,
) -> list[dict[str, str]]:
    if config is None:
        config = load_config()

    query_config = config.get("query", {})
    train_date = query_config.get("train_date") or datetime.now().strftime("%Y-%m-%d")
    from_station = query_config.get("from_station", "")
    to_station = query_config.get("to_station", "")

    if not train_date or not from_station or not to_station:
        if verbose:
            print("请先在配置中填写出发站、到达站和出发日期", flush=True)
        return []

    cache_key = (train_date, from_station, to_station, include_prices)
    with _QUERY_CACHE_LOCK:
        cached = _QUERY_CACHE.get(cache_key)
        if cached and (time.time() - cached[0] < QUERY_CACHE_TTL):
            if debug:
                print(
                    f"[query debug] cache hit key={cache_key} age={time.time() - cached[0]:.1f}s",
                    flush=True,
                )
            return copy.deepcopy(cached[1])

    if session is None:
        session = create_session()

    cookie_dict = login(session, verbose=False)
    if not cookie_dict:
        if verbose:
            print("Cookie已过期，请重新手动登录", flush=True)
        return []

    params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": from_station,
        "leftTicketDTO.to_station": to_station,
        "purpose_codes": "ADULT",
    }

    try:
        response = session.get(LEFT_TICKET_URL, params=params, timeout=10)
        if debug:
            preview = response.text[:200].replace("\n", " ").replace("\r", " ")
            print(f"[query debug] url={response.url}", flush=True)
            print(f"[query debug] raw_preview={preview}", flush=True)
        response.raise_for_status()
        response_json = response.json()
    except ValueError:
        if debug:
            preview = response.text[:200].replace("\n", " ").replace("\r", " ")
            print(f"[query debug] json parse failed raw_preview={preview}", flush=True)
        if verbose:
            if "error.html" in response.url:
                print("查票失败，12306 返回错误页，可能是日期尚未开售", flush=True)
            else:
                print("查票失败，接口返回格式异常", flush=True)
        return []
    except requests.RequestException as exc:
        if debug:
            print(f"[query debug] request failed error={exc}", flush=True)
        if verbose:
            print("查票失败，请稍后重试", flush=True)
        return []

    if not response_json.get("status", False):
        if verbose:
            print("查票失败，12306 未返回有效结果", flush=True)
        return []

    data = response_json.get("data", {})
    result_list = data.get("result", [])
    station_map = data.get("map", {})
    trains: list[dict[str, str]] = []

    for raw_result in result_list:
        train_info = _parse_train_result(raw_result, station_map)
        if train_info.get("can_web_buy") != "Y":
            continue
        trains.append(train_info)

    if include_prices and trains:
        enrich_ticket_prices(session, trains, train_date)

    with _QUERY_CACHE_LOCK:
        _QUERY_CACHE[cache_key] = (time.time(), copy.deepcopy(trains))

    if verbose:
        _print_train_list(trains)

    return trains


def month_matrix(year: int, month: int) -> list[list[int]]:
    cal = calendar.Calendar(firstweekday=0)
    return [list(week) for week in cal.monthdayscalendar(year, month)]
