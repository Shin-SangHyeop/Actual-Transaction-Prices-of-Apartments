import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import urllib.error
import urllib.request
from datetime import datetime, timezone

SOLAPI_SEND_URL = "https://api.solapi.com/messages/v4/send-many/detail"
KAKAO_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_REDIRECT_URI = "http://127.0.0.1:8765/oauth"
MAX_LMS_BYTES = 2000

class MessageSendError(RuntimeError):
    pass

def split_lms_text(text, max_bytes=MAX_LMS_BYTES):
    chunks, current = [], ""
    for line in text.splitlines():
        piece = line + "\n"
        if current and len((current + piece).encode("utf-8")) > max_bytes:
            chunks.append(current.rstrip())
            current = ""
        while len(piece.encode("utf-8")) > max_bytes:
            fragment = ""
            for char in piece:
                if len((fragment + char).encode("utf-8")) > max_bytes:
                    break
                fragment += char
            if not fragment:
                raise MessageSendError("LMS 메시지 분할에 실패했습니다.")
            chunks.append(fragment)
            piece = piece[len(fragment):]
        current += piece
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:max_bytes]]

def _authorization(api_key, api_secret):
    date_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = secrets.token_hex(16)
    signature = hmac.new(api_secret.encode("utf-8"),
                         (date_time + salt).encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return f"HMAC-SHA256 apiKey={api_key}, date={date_time}, salt={salt}, signature={signature}"

def send_lms(api_key, api_secret, sender, recipient, chunks):
    if not all((api_key, api_secret, sender, recipient)):
        raise MessageSendError("SOLAPI 키, 발신번호, 수신번호를 모두 설정해야 합니다.")
    sender = "".join(ch for ch in sender if ch.isdigit())
    recipient = "".join(ch for ch in recipient if ch.isdigit())
    if not sender or not recipient:
        raise MessageSendError("발신번호와 수신번호는 숫자로 입력해야 합니다.")
    if not chunks or any(len(chunk.encode("utf-8")) > MAX_LMS_BYTES for chunk in chunks):
        raise MessageSendError("LMS 본문이 허용 길이를 초과했습니다.")
    body = {"messages": [
        {"to": recipient, "from": sender, "text": chunk, "type": "LMS",
         "country": "82", "subject": "아파트 실거래 요약"}
        for chunk in chunks
    ], "strict": True, "allowDuplicates": False, "showMessageList": True}
    request = urllib.request.Request(
        SOLAPI_SEND_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": _authorization(api_key, api_secret),
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = detail.get("errorMessage", detail.get("statusMessage", str(exc)))
        except (ValueError, AttributeError):
            detail = str(exc)
        raise MessageSendError(f"SOLAPI 요청 실패: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MessageSendError(f"SOLAPI 응답을 확인할 수 없습니다: {exc}") from exc
    if result.get("errorCode"):
        raise MessageSendError(result.get("errorMessage", "SOLAPI 발송 요청이 거부되었습니다."))
    failed = result.get("failedMessageList", [])
    counts = (result.get("groupInfo") or {}).get("count", {})
    accepted = counts.get("registeredSuccess")
    if failed or accepted != len(chunks):
        reasons = "; ".join(
            f"{item.get('statusCode', '')} {item.get('statusMessage', '')}".strip()
            for item in failed)
        raise MessageSendError(
            f"일부 LMS가 접수되지 않았습니다. 접수 {accepted or 0}/{len(chunks)}건. {reasons}")
    return result.get("groupInfo", {}).get("groupId", "")


def send_kakao_alimtalk(api_key, api_secret, recipient, pf_id, template_id, chunks):
    """Send report chunks with an approved SOLAPI Kakao AlimTalk template.

    The approved template must contain the exact substitution token #{보고서}.
    SMS fallback is disabled so a Kakao failure does not silently incur SMS cost.
    """
    if not all((api_key, api_secret, recipient, pf_id, template_id)):
        raise MessageSendError("SOLAPI API Key/Secret, 수신번호, 카카오 채널 ID와 승인된 템플릿 ID를 모두 설정하세요.")
    recipient = "".join(ch for ch in recipient if ch.isdigit())
    if not recipient:
        raise MessageSendError("수신번호는 숫자로 입력하세요.")
    if not chunks or any(len(chunk) > 1000 for chunk in chunks):
        raise MessageSendError("알림톡 본문은 템플릿 변수 치환 후 1,000자를 넘을 수 없습니다.")

    body = {"messages": [
        {"to": recipient, "country": "82", "type": "ATA",
         "kakaoOptions": {
             "pfId": pf_id,
             "templateId": template_id,
             "variables": {"#{보고서}": chunk},
             "disableSms": True,
         }}
        for chunk in chunks
    ], "strict": True, "allowDuplicates": True, "showMessageList": True}
    request = urllib.request.Request(
        SOLAPI_SEND_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": _authorization(api_key, api_secret),
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = detail.get("errorMessage", detail.get("statusMessage", str(exc)))
        except (ValueError, AttributeError):
            detail = str(exc)
        raise MessageSendError(f"SOLAPI 카카오 알림톡 요청 실패: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MessageSendError(f"SOLAPI 응답을 확인할 수 없습니다: {exc}") from exc
    if result.get("errorCode"):
        raise MessageSendError(result.get("errorMessage", "SOLAPI가 알림톡 요청을 거부했습니다."))

    failed = result.get("failedMessageList", [])
    counts = (result.get("groupInfo") or {}).get("count", {})
    accepted = counts.get("registeredSuccess")
    if failed or accepted != len(chunks):
        reasons = "; ".join(
            f"{item.get('statusCode', '')} {item.get('statusMessage', '')}".strip()
            for item in failed)
        raise MessageSendError(
            f"알림톡 {accepted or 0}/{len(chunks)}건만 접수되었습니다. {reasons}")
    return result.get("groupInfo", {}).get("groupId", "")


def _kakao_form_post(url, values, access_token=None):
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, data=urlencode(values).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = detail.get("msg", detail.get("message", detail.get("error_description", str(exc))))
        except (ValueError, AttributeError):
            message = str(exc)
        raise MessageSendError(f"카카오 API 요청 실패: {message}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MessageSendError(f"카카오 서버 응답을 확인할 수 없습니다: {exc}") from exc


def _open_kakao_in_chrome(url):
    """Open Kakao OAuth in Chrome on Windows, including common install paths."""
    candidates = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for chrome_path in candidates:
        if chrome_path.is_file():
            subprocess.Popen([str(chrome_path), url])
            return
    try:
        chrome = webbrowser.get("chrome")
        if chrome.open(url):
            return
    except webbrowser.Error:
        pass
    raise MessageSendError("Chrome을 찾을 수 없습니다. Chrome 설치를 확인한 뒤 다시 시도하세요.")


def authorize_kakao(rest_api_key, client_secret="", timeout=180):
    """Run the Kakao OAuth consent flow using a loopback desktop callback."""
    if not rest_api_key:
        raise MessageSendError("카카오 REST API 키를 입력하세요.")
    state = secrets.token_urlsafe(24)
    callback = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/oauth":
                self.send_error(404)
                return
            if query.get("state", [""])[0] != state:
                callback["error"] = "카카오 로그인 state 검증에 실패했습니다. 다시 연결하세요."
            elif query.get("error"):
                callback["error"] = query.get("error_description", query["error"])[0]
            else:
                callback["code"] = query.get("code", [""])[0]
            payload = "카카오 연결이 완료되었습니다. 이 창을 닫고 앱으로 돌아가세요.".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", 8765), CallbackHandler)
    except OSError as exc:
        raise MessageSendError("카카오 로그인용 로컬 포트 8765를 열 수 없습니다. 앱을 다시 시작한 뒤 연결하세요.") from exc
    server.timeout = 1
    params = {"client_id": rest_api_key, "redirect_uri": KAKAO_REDIRECT_URI,
              "response_type": "code", "scope": "talk_message", "state": state}
    try:
        _open_kakao_in_chrome(f"{KAKAO_AUTHORIZE_URL}?{urlencode(params)}")
        deadline = time.monotonic() + timeout
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if callback.get("error"):
        raise MessageSendError(f"카카오 로그인 실패: {callback['error']}")
    if not callback.get("code"):
        raise MessageSendError("카카오 로그인이 시간 초과되었습니다. 다시 연결하세요.")
    values = {"grant_type": "authorization_code", "client_id": rest_api_key,
              "redirect_uri": KAKAO_REDIRECT_URI, "code": callback["code"]}
    if client_secret:
        values["client_secret"] = client_secret
    tokens = _kakao_form_post(KAKAO_TOKEN_URL, values)
    tokens["access_token_expires_at"] = time.time() + int(tokens.get("expires_in", 0))
    tokens["client_id"] = rest_api_key
    tokens["client_secret"] = client_secret
    return tokens


def _ensure_kakao_access_token(kakao, on_tokens=None):
    tokens = dict(kakao)
    if tokens.get("access_token") and float(tokens.get("access_token_expires_at", 0)) > time.time() + 60:
        return tokens
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise MessageSendError("카카오 계정 연결이 필요합니다. '카카오 연결'을 눌러 다시 로그인하세요.")
    values = {"grant_type": "refresh_token", "client_id": tokens.get("client_id", ""),
              "refresh_token": refresh_token}
    if tokens.get("client_secret"):
        values["client_secret"] = tokens["client_secret"]
    refreshed = _kakao_form_post(KAKAO_TOKEN_URL, values)
    tokens.update(refreshed)
    tokens["access_token_expires_at"] = time.time() + int(refreshed.get("expires_in", 0))
    if on_tokens:
        on_tokens(tokens)
    return tokens


def send_kakao_memo(kakao, messages, on_tokens=None):
    """Send text chunks to the authenticated user's KakaoTalk memo chat."""
    if not messages or any(len(message) > 200 for message in messages):
        raise MessageSendError("카카오 텍스트 메시지는 한 건당 200자 이하여야 합니다.")
    tokens = _ensure_kakao_access_token(kakao, on_tokens)
    if not tokens.get("client_id"):
        raise MessageSendError("카카오 REST API 키가 없습니다. 카카오 연결을 다시 진행하세요.")
    for index, message in enumerate(messages, start=1):
        template = {"object_type": "text", "text": message,
                    "link": {"web_url": "https://developers.kakao.com",
                             "mobile_web_url": "https://developers.kakao.com"}}
        try:
            result = _kakao_form_post(KAKAO_MEMO_SEND_URL,
                {"template_object": json.dumps(template, ensure_ascii=False)},
                tokens["access_token"])
        except MessageSendError as exc:
            raise MessageSendError(f"카카오 메시지 {index}/{len(messages)}건 전송 중 오류: {exc}") from exc
        if result.get("result_code") != 0:
            raise MessageSendError(f"카카오 메시지 {index}/{len(messages)}건 전송 실패: {result}")
    return f"{len(messages)}건"
