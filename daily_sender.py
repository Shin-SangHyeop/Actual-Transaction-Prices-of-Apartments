"""Headless Kakao report sender for the scheduled GitHub Actions workflow."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from cryptography.fernet import Fernet, InvalidToken

import app
from messaging import send_kakao_memo


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "private-state" / "user_settings.enc"


def _fernet():
    key = os.environ.get("KAKAO_STATE_KEY", "")
    if not key:
        raise RuntimeError("KAKAO_STATE_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("KAKAO_STATE_KEY is invalid") from exc


def _load_user_settings(fernet):
    if STATE_FILE.exists():
        try:
            return fernet.decrypt(STATE_FILE.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError("Encrypted Kakao state could not be decrypted") from exc

    initial_settings = os.environ.get("APP_SETTINGS_YAML", "")
    if not initial_settings:
        raise RuntimeError("APP_SETTINGS_YAML is required for the first run")
    return initial_settings.encode("utf-8")


def _persist_encrypted_state(fernet):
    if not app.USER_CONFIG.exists():
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_bytes(fernet.encrypt(app.USER_CONFIG.read_bytes()))
    app.USER_CONFIG.unlink()


def main():
    fernet = _fernet()
    app.USER_CONFIG.write_bytes(_load_user_settings(fernet))
    try:
        cfg = app.read_config()
        apartments = cfg.get("apartments", [])
        if not apartments:
            raise RuntimeError("No apartments are configured")
        if not cfg.get("molit_service_key"):
            raise RuntimeError("The public data API key is not configured")

        korea_time = timezone(timedelta(hours=9))
        today = datetime.now(korea_time).date()
        results = []
        for apartment in apartments:
            rows = app.fetch(apartment, 3, cfg["molit_service_key"], today=today)
            results.append((apartment["name"], rows))

        kakao = cfg.get("kakao", {})
        if not kakao.get("refresh_token") and not kakao.get("access_token"):
            raise RuntimeError("Kakao authorization is not configured")

        def persist_tokens(tokens):
            user_cfg = yaml.safe_load(
                app.USER_CONFIG.read_text(encoding="utf-8-sig")) or {}
            user_cfg["kakao"] = tokens
            app.write_user_config(user_cfg)

        messages = app.make_kakao_messages(results, queried_on=today, months=3)
        result = send_kakao_memo(kakao, messages, on_tokens=persist_tokens)
        print(f"Kakao report sent successfully ({result}).")
        return 0
    except Exception as exc:
        # Avoid writing API URLs or credential-bearing request details to public logs.
        print(f"Scheduled Kakao send failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    finally:
        _persist_encrypted_state(fernet)


if __name__ == "__main__":
    raise SystemExit(main())
