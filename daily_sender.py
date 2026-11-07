"""Headless Kakao report sender for the scheduled GitHub Actions workflow."""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from cryptography.fernet import Fernet, InvalidToken

import app
from messaging import send_kakao_memo

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")


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
    encoded_settings = os.environ.get("APP_SETTINGS_YAML_B64", "")
    if not encoded_settings:
        raise RuntimeError("APP_SETTINGS_YAML_B64 is required")
    try:
        settings_text = base64.b64decode(
            encoded_settings, validate=True).decode("utf-8-sig")
        user_cfg = yaml.safe_load(settings_text) or {}
    except (ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeError("APP_SETTINGS_YAML_B64 is invalid") from exc
    if not isinstance(user_cfg, dict):
        raise RuntimeError("APP_SETTINGS_YAML_B64 must contain a mapping")

    seed_kakao = user_cfg.get("kakao", {}) or {}
    kakao_seed_hash = hashlib.sha256(json.dumps(
        seed_kakao, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    if STATE_FILE.exists():
        try:
            saved_state = yaml.safe_load(
                fernet.decrypt(STATE_FILE.read_bytes())) or {}
        except InvalidToken as exc:
            raise RuntimeError("Encrypted Kakao state could not be decrypted") from exc
        if saved_state.get("kakao_seed_hash") == kakao_seed_hash:
            user_cfg["kakao"] = saved_state.get("kakao", seed_kakao)
        elif "kakao_seed_hash" not in saved_state:
            # Migrate the previous encrypted full-settings format.
            saved_kakao = saved_state.get("kakao", {}) or {}
            if saved_kakao.get("client_id") == seed_kakao.get("client_id"):
                user_cfg["kakao"] = saved_kakao
    return user_cfg, kakao_seed_hash


def _persist_encrypted_state(fernet, kakao, kakao_seed_hash):
    if not app.USER_CONFIG.exists():
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {"kakao": kakao, "kakao_seed_hash": kakao_seed_hash}
    encrypted = fernet.encrypt(yaml.safe_dump(
        state, allow_unicode=True, sort_keys=False).encode("utf-8"))
    STATE_FILE.write_bytes(encrypted)
    app.USER_CONFIG.unlink()


def main():
    fernet = _fernet()
    user_cfg, kakao_seed_hash = _load_user_settings(fernet)
    app.USER_CONFIG.write_text(yaml.safe_dump(
        user_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cfg = None
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
            cfg["kakao"] = tokens
            current_user_cfg = yaml.safe_load(
                app.USER_CONFIG.read_text(encoding="utf-8-sig")) or {}
            current_user_cfg["kakao"] = tokens
            app.write_user_config(current_user_cfg)

        messages = app.make_kakao_messages(results, queried_on=today, months=3)
        send_kakao_memo(kakao, messages, on_tokens=persist_tokens)
        print(f"Kakao report sent successfully ({len(messages)} message(s)).")
        return 0
    except Exception as exc:
        # Avoid writing API URLs or credential-bearing request details to public logs.
        print(f"Scheduled Kakao send failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    finally:
        kakao = cfg.get("kakao", {}) if cfg is not None else user_cfg.get("kakao", {})
        _persist_encrypted_state(fernet, kakao, kakao_seed_hash)


if __name__ == "__main__":
    raise SystemExit(main())
