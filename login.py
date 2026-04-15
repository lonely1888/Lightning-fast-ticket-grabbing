import base64
import json
import sys
from pathlib import Path

import requests
from gmssl.sm4 import CryptSM4, SM4_ENCRYPT

from paths import get_runtime_file


COOKIE_FILE = get_runtime_file("cookies.txt")
LOGIN_CHECK_URL = "https://kyfw.12306.cn/otn/login/conf"
WEB_LOGIN_URL = "https://kyfw.12306.cn/passport/web/login"
CHECK_LOGIN_VERIFY_URL = "https://kyfw.12306.cn/passport/web/checkLoginVerify"
CHECK_USER_INFO_URL = "https://kyfw.12306.cn/passport/web/checkUserInfo"
USER_LOGIN_FOR_ICCARD_URL = "https://kyfw.12306.cn/passport/web/userLoginForIccard"
GET_MESSAGE_CODE_URL = "https://kyfw.12306.cn/passport/web/getMessageCode"
AUTH_UAMTK_URL = "https://kyfw.12306.cn/passport/web/auth/uamtk"
UAM_AUTH_CLIENT_URL = "https://kyfw.12306.cn/otn/uamauthclient"
PASSPORT_APP_ID = "otn"
SM4_KEY = "tiekeyuankp12306"
LOGIN_CONTEXT_ATTR = "_ticket_login_context"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def parse_cookie_string(cookie_text: str) -> dict[str, str]:
    cookie_dict: dict[str, str] = {}
    normalized = cookie_text.replace("\r", "").replace("\n", " ").strip()

    for item in normalized.split(";"):
        pair = item.strip()
        if not pair or "=" not in pair:
            continue

        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookie_dict[key] = value

    return cookie_dict


def load_cookie_text(cookie_file: str | Path = COOKIE_FILE) -> str:
    path = Path(cookie_file)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_cookie_text(cookie_text: str, cookie_file: str | Path = COOKIE_FILE) -> None:
    Path(cookie_file).write_text(cookie_text.strip(), encoding="utf-8")


def dump_cookie_text(session: requests.Session) -> str:
    cookie_dict = session.cookies.get_dict()
    return "; ".join(f"{key}={value}" for key, value in cookie_dict.items())


def load_cookies(cookie_file: str | Path = COOKIE_FILE, verbose: bool = True) -> dict[str, str]:
    cookie_text = load_cookie_text(cookie_file)
    if not cookie_text:
        if verbose:
            print("Cookie 已过期，请重新手动登录", flush=True)
        return {}
    return parse_cookie_string(cookie_text)


def create_session(user_agent: str = DEFAULT_USER_AGENT) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.12306.cn/index/",
            "Origin": "https://www.12306.cn",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def encrypt_password(password: str) -> str:
    crypt_sm4 = CryptSM4()
    crypt_sm4.set_key(SM4_KEY.encode("utf-8"), SM4_ENCRYPT)
    encrypted = crypt_sm4.crypt_ecb(password.encode("utf-8"))
    return "@" + base64.b64encode(encrypted).decode("utf-8")


def _extract_error_message(response_json: dict, default: str) -> str:
    for key in ("result_message", "message", "msg"):
        value = response_json.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    messages = response_json.get("messages")
    if isinstance(messages, list):
        for item in messages:
            text = str(item).strip()
            if text:
                return text
    if isinstance(messages, str) and messages.strip():
        return messages.strip()

    data = response_json.get("data", {})
    if isinstance(data, dict):
        for key in ("result_message", "message", "msg", "errMsg"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return default


def _response_text(response: requests.Response) -> str:
    return response.text.replace("\ufeff", "").strip()


def _debug_response(step_label: str, response: requests.Response) -> None:
    snippet = _response_text(response).replace("\r", " ").replace("\n", " ")
    output_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_snippet = snippet[:300].encode(output_encoding, errors="backslashreplace").decode(output_encoding)
    print(f"[debug] 步骤: {step_label} {response.request.method} {response.url}", flush=True)
    print("[debug] 状态码:", response.status_code, flush=True)
    print("[debug] 响应内容:", safe_snippet, flush=True)


def _request(
    session: requests.Session,
    method: str,
    url: str,
    step_label: str,
    **kwargs,
) -> tuple[bool, requests.Response | None, str]:
    try:
        response = session.request(method=method, url=url, timeout=10, **kwargs)
    except requests.RequestException as exc:
        return False, None, f"{step_label} 请求失败: {exc}"

    _debug_response(step_label, response)

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        return False, response, f"{step_label} HTTP 错误: {exc}"

    return True, response, ""


def _parse_json_response(
    response: requests.Response,
    step_label: str,
) -> tuple[bool, dict, str]:
    text = _response_text(response)
    if not text:
        return False, {}, f"{step_label} 接口返回空响应"

    try:
        parsed = json.loads(text)
    except ValueError:
        preview = text[:120]
        return False, {}, f"{step_label} 返回的不是 JSON: {preview}"

    if not isinstance(parsed, dict):
        return False, {}, f"{step_label} 返回了非对象 JSON"

    return True, parsed, ""


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    step_label: str,
    **kwargs,
) -> tuple[bool, dict, str]:
    ok, response, error_message = _request(session, method, url, step_label, **kwargs)
    if not ok or response is None:
        return False, {}, error_message
    return _parse_json_response(response, step_label)


def _is_success_code(value: object, *extra_success_codes: object) -> bool:
    normalized = str(value)
    success_codes = {"0"}
    success_codes.update(str(code) for code in extra_success_codes)
    return normalized in success_codes


def _build_base_login_payload(username: str, password: str) -> dict[str, str]:
    return {
        "sessionId": "",
        "sig": "",
        "if_check_slide_passcode_token": "",
        "scene": "",
        "checkMode": "",
        "randCode": "",
        "username": username.strip(),
        "password": encrypt_password(password),
        "appid": PASSPORT_APP_ID,
    }


def _store_login_context(session: requests.Session, context: dict) -> None:
    setattr(session, LOGIN_CONTEXT_ATTR, context)


def _load_login_context(session: requests.Session) -> dict:
    context = getattr(session, LOGIN_CONTEXT_ATTR, {})
    return context if isinstance(context, dict) else {}


def _clear_login_context(session: requests.Session) -> None:
    _store_login_context(session, {})


def _initialize_login_session(session: requests.Session) -> tuple[bool, dict, str]:
    return _request_json(session, "GET", LOGIN_CHECK_URL, "第1步 初始化session")


def _submit_account_password(
    username: str,
    password: str,
    session: requests.Session,
) -> tuple[bool, dict, str]:
    payload = _build_base_login_payload(username, password)
    return _request_json(
        session,
        "POST",
        WEB_LOGIN_URL,
        "第2步 提交账号密码",
        data=payload,
    )


def _check_login_verify(
    username: str,
    session: requests.Session,
) -> tuple[bool, dict, str]:
    payload = {"username": username.strip(), "appid": PASSPORT_APP_ID}
    return _request_json(
        session,
        "POST",
        CHECK_LOGIN_VERIFY_URL,
        "兼容检查 checkLoginVerify",
        data=payload,
    )


def _request_sms_code_legacy(
    username: str,
    id_suffix: str,
    session: requests.Session,
) -> tuple[bool, dict, str]:
    payload = {
        "appid": PASSPORT_APP_ID,
        "username": username.strip(),
        "castNum": id_suffix.strip(),
    }
    return _request_json(
        session,
        "POST",
        CHECK_USER_INFO_URL,
        "第3步 发送短信(checkUserInfo)",
        data=payload,
    )


def _request_sms_code_current(
    username: str,
    id_suffix: str,
    session: requests.Session,
) -> tuple[bool, dict, str]:
    payload = {
        "appid": PASSPORT_APP_ID,
        "username": username.strip(),
        "castNum": id_suffix.strip(),
    }
    return _request_json(
        session,
        "POST",
        GET_MESSAGE_CODE_URL,
        "第3步 发送短信(getMessageCode兼容)",
        data=payload,
    )


def _submit_sms_login_legacy(
    username: str,
    password: str,
    sms_code: str,
    session: requests.Session,
) -> tuple[bool, dict, str]:
    payload = _build_base_login_payload(username, password)
    payload["checkMode"] = "0"
    payload["randCode"] = sms_code.strip()
    return _request_json(
        session,
        "POST",
        USER_LOGIN_FOR_ICCARD_URL,
        "第4步 提交验证码(userLoginForIccard)",
        data=payload,
    )


def _submit_sms_login_current(
    username: str,
    password: str,
    sms_code: str,
    session: requests.Session,
) -> tuple[bool, dict, str]:
    payload = _build_base_login_payload(username, password)
    payload["checkMode"] = "0"
    payload["randCode"] = sms_code.strip()
    return _request_json(
        session,
        "POST",
        WEB_LOGIN_URL,
        "第4步 提交验证码(web/login兼容)",
        data=payload,
    )


def _exchange_uamtk(session: requests.Session) -> tuple[bool, dict, str]:
    return _request_json(
        session,
        "POST",
        AUTH_UAMTK_URL,
        "第5步 获取token",
        data={"appid": PASSPORT_APP_ID},
    )


def _exchange_otn_cookie(session: requests.Session, token: str) -> tuple[bool, dict, str]:
    return _request_json(
        session,
        "POST",
        UAM_AUTH_CLIENT_URL,
        "第6步 换取otn Cookie",
        data={"tk": token},
    )


def check_login_verify(username: str, session: requests.Session | None = None) -> tuple[bool, dict]:
    if session is None:
        session = create_session()

    ok, response_json, _ = _check_login_verify(username, session=session)
    if not ok:
        return False, {}
    return True, response_json


def request_sms_code(
    username: str,
    password: str,
    id_suffix: str,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict]:
    if session is None:
        session = create_session()

    _clear_login_context(session)

    ok, _, error_message = _initialize_login_session(session)
    if not ok:
        return False, error_message, {}

    prelogin_ok, prelogin_json, prelogin_error = _submit_account_password(
        username=username,
        password=password,
        session=session,
    )

    verify_ok, verify_json, _ = _check_login_verify(username=username, session=session)

    context = {
        "username": username.strip(),
        "password": password,
        "id_suffix": id_suffix.strip(),
        "prelogin_ok": prelogin_ok,
        "prelogin_response": prelogin_json,
        "check_login_verify": verify_json if verify_ok else {},
    }
    _store_login_context(session, context)

    legacy_ok, legacy_json, legacy_error = _request_sms_code_legacy(
        username=username,
        id_suffix=id_suffix,
        session=session,
    )
    if legacy_ok and _is_success_code(legacy_json.get("result_code")):
        context["sms_request_response"] = legacy_json
        context["sms_request_endpoint"] = "checkUserInfo"
        _store_login_context(session, context)
        message = _extract_error_message(legacy_json, "短信验证码已发送")
        return True, message, legacy_json

    current_ok, current_json, current_error = _request_sms_code_current(
        username=username,
        id_suffix=id_suffix,
        session=session,
    )
    if current_ok and _is_success_code(current_json.get("result_code")):
        context["sms_request_response"] = current_json
        context["sms_request_endpoint"] = "getMessageCode"
        _store_login_context(session, context)
        message = _extract_error_message(current_json, "短信验证码已发送")
        return True, message, current_json

    if current_ok:
        message = _extract_error_message(current_json, "获取短信验证码失败")
        context["sms_request_response"] = current_json
        context["sms_request_endpoint"] = "getMessageCode"
    elif legacy_ok:
        message = _extract_error_message(legacy_json, "获取短信验证码失败")
        context["sms_request_response"] = legacy_json
        context["sms_request_endpoint"] = "checkUserInfo"
    elif prelogin_ok:
        message = _extract_error_message(prelogin_json, "获取短信验证码失败")
    else:
        message = current_error or legacy_error or prelogin_error or "获取短信验证码失败"

    _store_login_context(session, context)
    return False, message, context


def login_with_sms_code(
    username: str,
    password: str,
    sms_code: str,
    id_suffix: str = "",
    session: requests.Session | None = None,
    cookie_file: str | Path = COOKIE_FILE,
) -> tuple[bool, str, dict[str, str]]:
    if session is None:
        session = create_session()

    context = _load_login_context(session)
    if context.get("username") != username.strip() or context.get("password") != password:
        _clear_login_context(session)
        init_ok, _, init_error = _initialize_login_session(session)
        if not init_ok:
            return False, init_error, {}

        prelogin_ok, prelogin_json, prelogin_error = _submit_account_password(
            username=username,
            password=password,
            session=session,
        )
        if not prelogin_ok:
            return False, prelogin_error, {}

        context = {
            "username": username.strip(),
            "password": password,
            "id_suffix": id_suffix.strip(),
            "prelogin_response": prelogin_json,
        }
        _store_login_context(session, context)

    legacy_ok, legacy_json, legacy_error = _submit_sms_login_legacy(
        username=username,
        password=password,
        sms_code=sms_code,
        session=session,
    )
    if legacy_ok and _is_success_code(legacy_json.get("result_code")):
        login_json = legacy_json
    else:
        current_ok, current_json, current_error = _submit_sms_login_current(
            username=username,
            password=password,
            sms_code=sms_code,
            session=session,
        )
        if not current_ok:
            return False, current_error or legacy_error or "登录请求失败", {}
        if not _is_success_code(current_json.get("result_code")):
            return False, _extract_error_message(current_json, "登录失败，请检查账号、密码或短信验证码"), {}
        login_json = current_json

    auth_ok, auth_json, auth_error = _exchange_uamtk(session)
    if not auth_ok:
        return False, auth_error, {}

    if not _is_success_code(auth_json.get("result_code"), 91, 92):
        return False, _extract_error_message(auth_json, "登录成功但获取 token 失败"), {}

    newapptk = str(
        auth_json.get("newapptk")
        or auth_json.get("apptk")
        or auth_json.get("data", {}).get("newapptk", "")
        or auth_json.get("data", {}).get("apptk", "")
    )
    if not newapptk:
        return False, _extract_error_message(auth_json, "登录成功但未拿到 token"), {}

    client_ok, client_json, client_error = _exchange_otn_cookie(session, newapptk)
    if not client_ok:
        return False, client_error, {}

    client_success = _is_success_code(client_json.get("result_code")) or bool(client_json.get("status"))
    if not client_success:
        return False, _extract_error_message(client_json, "票务授权失败"), {}

    cookie_dict = session.cookies.get_dict()
    is_valid, _ = validate_cookie_dict(cookie_dict, session=session)
    if not is_valid:
        return False, "登录成功但 Cookie 校验未通过，请稍后重试", {}

    save_cookie_text(dump_cookie_text(session), cookie_file=cookie_file)
    return True, _extract_error_message(login_json, "登录成功，Cookie 已保存"), cookie_dict


def _is_login_valid(response_json: dict) -> bool:
    if not response_json.get("status", False):
        return False

    data = response_json.get("data", {})
    is_login = data.get("is_login")
    return is_login in {"Y", "y", True, 1, "1"}


def validate_cookie_dict(
    cookie_dict: dict[str, str],
    session: requests.Session | None = None,
) -> tuple[bool, dict]:
    if not cookie_dict:
        return False, {}

    if session is None:
        session = create_session()

    session.cookies.clear()
    session.cookies.update(cookie_dict)

    ok, response_json, _ = _request_json(
        session,
        "GET",
        LOGIN_CHECK_URL,
        "Cookie 校验",
    )
    if not ok:
        return False, {}

    return _is_login_valid(response_json), response_json


def validate_cookie_text(
    cookie_text: str,
    session: requests.Session | None = None,
) -> tuple[bool, dict[str, str]]:
    cookie_dict = parse_cookie_string(cookie_text)
    is_valid, _ = validate_cookie_dict(cookie_dict, session=session)
    return is_valid, cookie_dict


def login(
    session: requests.Session | None = None,
    cookie_file: str | Path = COOKIE_FILE,
    verbose: bool = True,
) -> dict[str, str]:
    cookie_dict = load_cookies(cookie_file, verbose=verbose)
    is_valid, _ = validate_cookie_dict(cookie_dict, session=session)
    if is_valid:
        if verbose:
            print("登录验证成功", flush=True)
        return cookie_dict

    if verbose:
        print("Cookie 已过期，请重新手动登录", flush=True)
    return {}
