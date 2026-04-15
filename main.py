from datetime import datetime, time, timedelta
from time import sleep

from loguru import logger

from login import create_session, login
from order import place_order
from query import has_enough_inventory, load_config, query_tickets


PREHEAT_START = time(7, 50, 0)
PREHEAT_END = time(7, 59, 59)
SPRINT_START = time(8, 0, 0)
SPRINT_END = time(8, 5, 59)

PREHEAT_INTERVAL = 10.0
SPRINT_INTERVAL = 0.5
DEFAULT_INTERVAL = 15.0
FAILURE_BACKOFF_MULTIPLIER = 1.8
FAILURE_BACKOFF_CAP = 30.0
MIN_FAILURE_INTERVAL = 2.0


def _find_target_train(trains: list[dict], target_depart_time: str) -> dict | None:
    for train in trains:
        if train.get("depart_time") == target_depart_time:
            return train
    return None


def _format_status_line(target_train: dict | None, target_depart_time: str) -> str:
    if not target_train:
        return f"[{target_depart_time}车次] 未找到目标车次"

    second_class = target_train.get("second_class", "") or "-"
    return f"[{target_depart_time}车次] 二等座余票：{second_class}张"


def _get_stage_plan(now: datetime) -> tuple[str, float]:
    current_time = now.time()
    if PREHEAT_START <= current_time <= PREHEAT_END:
        return "预热阶段", PREHEAT_INTERVAL
    if SPRINT_START <= current_time <= SPRINT_END:
        return "冲刺阶段", SPRINT_INTERVAL
    return "常规阶段", DEFAULT_INTERVAL


def _format_seconds(seconds: float) -> str:
    if seconds.is_integer():
        return f"{int(seconds)}秒"
    return f"{seconds:.1f}秒"


def _compute_retry_interval(base_interval: float, consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return base_interval

    retry_interval = base_interval * (FAILURE_BACKOFF_MULTIPLIER ** consecutive_failures)
    retry_interval = min(retry_interval, FAILURE_BACKOFF_CAP)
    retry_interval = max(retry_interval, MIN_FAILURE_INTERVAL)
    return max(base_interval, retry_interval)


def _format_next_run(now: datetime, seconds: float) -> str:
    return (now + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def run_once(session) -> dict:
    config = load_config()
    query_config = config.get("query", {})
    target_depart_time = str(query_config.get("target_depart_time", "")).strip()
    passenger_count = int(query_config.get("passenger_count", 1))

    try:
        trains = query_tickets(session=session, config=config, verbose=False)
    except Exception as exc:
        logger.exception("查询流程异常: {}", exc)
        return {"stop": False, "status": "error", "message": str(exc)}

    target_train = _find_target_train(trains, target_depart_time)

    logger.info(
        "查询完成 target_depart_time={} train_count={} target_found={}",
        target_depart_time,
        len(trains),
        bool(target_train),
    )
    print(_format_status_line(target_train, target_depart_time), flush=True)

    if not target_train:
        return {"stop": False, "status": "ok", "message": "目标车次未出现"}

    second_class = target_train.get("second_class", "")
    logger.info(
        "目标车次={} 出发={} 到达={} 二等座={}",
        target_train.get("train_code"),
        target_train.get("depart_time"),
        target_train.get("arrive_time"),
        second_class,
    )

    if not has_enough_inventory(second_class, passenger_count):
        return {"stop": False, "status": "ok", "message": "二等座余票不足"}

    print(
        f"找到目标车次，余票 {second_class} 张，开始尝试下单",
        flush=True,
    )
    logger.success(
        "命中目标车次={} second_class={} passenger_count={}，开始真实下单",
        target_train.get("train_code"),
        second_class,
        passenger_count,
    )

    result = place_order(session, config, target_train)
    if result.get("success"):
        print("抢票成功，请在30分钟内登录12306完成支付", flush=True)
        logger.success(result["message"])
        return {"stop": True, "status": "ok", "message": result["message"]}

    logger.error("下单失败: {}", result["message"])
    return {
        "stop": not result.get("retryable", True),
        "status": "error" if result.get("retryable", True) else "fatal",
        "message": result["message"],
    }


def main() -> None:
    logger.add("runtime.log", rotation="1 MB", encoding="utf-8")

    session = create_session()
    cookie_dict = login(session, verbose=False)
    if not cookie_dict:
        print("Cookie 已过期，请重新手动登录", flush=True)
        logger.error("Cookie 已过期，无法启动抢票循环")
        return

    print("真实抢票模式已启动，按分时段策略查询", flush=True)
    logger.info("真实抢票模式已启动，按分时段策略查询")

    consecutive_failures = 0

    try:
        while True:
            result = run_once(session)
            if result.get("stop"):
                break

            if result.get("status") == "fatal":
                logger.error("遇到不可重试错误，停止运行: {}", result.get("message", "未知错误"))
                break

            if result.get("status") == "error":
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            now = datetime.now()
            stage_name, base_interval = _get_stage_plan(now)
            wait_seconds = _compute_retry_interval(base_interval, consecutive_failures)
            next_run_at = _format_next_run(now, wait_seconds)

            logger.info(
                "当前阶段={} 基础间隔={} 连续失败={} 实际等待={} 下次查询时间={}",
                stage_name,
                _format_seconds(base_interval),
                consecutive_failures,
                _format_seconds(wait_seconds),
                next_run_at,
            )

            if consecutive_failures > 0:
                logger.warning(
                    "检测到查询/下单失败，已启用退避重试: 连续失败={} 下次查询时间={}",
                    consecutive_failures,
                    next_run_at,
                )

            sleep(wait_seconds)
    except KeyboardInterrupt:
        logger.warning("已手动停止抢票循环")
        print("已手动停止抢票循环", flush=True)


if __name__ == "__main__":
    main()
