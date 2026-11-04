import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone

SOLAPI_SEND_URL = "https://api.solapi.com/messages/v4/send-many/detail"
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
