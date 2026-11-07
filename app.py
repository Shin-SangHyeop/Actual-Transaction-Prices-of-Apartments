import json, queue, subprocess, threading, urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree as ET
import yaml
from messaging import MessageSendError, authorize_kakao, send_kakao_memo, send_lms, split_lms_text
from datetime import date
from calendar import monthrange
from pathlib import Path
from types import MappingProxyType
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "settings.conf"
USER_CONFIG = ROOT / "user_settings.yaml"
TRADE = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
RENT = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
DEFAULT = {"lookback_months": 3, "auto_search_on_start": True, "apartments": [],
           "molit_service_key": "", "notification": {"provider": "none", "recipient": ""},
           "shutdown_after_send": False}

def read_config():
    if not CONFIG.exists():
        write_config(DEFAULT)
    app_cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    # Create the per-user file on first launch. The repository may contain an
    # existing settings.conf while user_settings.yaml is intentionally absent.
    if not USER_CONFIG.exists():
        write_user_config({})
    user_cfg = yaml.safe_load(USER_CONFIG.read_text(encoding="utf-8-sig")) or {}
    cfg = {**DEFAULT, **app_cfg}
    cfg["apartments"] = user_cfg.get("apartments", [])
    cfg["molit_service_key"] = user_cfg.get("molit_service_key", "")
    cfg["solapi"] = user_cfg.get("solapi", {})
    cfg["kakao"] = user_cfg.get("kakao", {})
    return cfg

def write_config(cfg):
    app_cfg = {k: v for k, v in cfg.items() if k not in ("apartments", "molit_service_key", "solapi", "kakao")}
    user_cfg = {"molit_service_key": cfg.get("molit_service_key", ""),
                "apartments": cfg.get("apartments", []),
                "solapi": cfg.get("solapi", {}), "kakao": cfg.get("kakao", {})}
    CONFIG.write_text(json.dumps(app_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_user_config(user_cfg)

def write_user_config(user_cfg):
    USER_CONFIG.write_text(yaml.safe_dump(user_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

def lookback_start(months, today=None):
    today = today or date.today()
    month_index = today.year * 12 + today.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(today.day, monthrange(year, month)[1])
    return date(year, month, day)


def recent_months(n, today=None):
    today = today or date.today()
    start = lookback_start(n, today)
    y, m = today.year, today.month
    out = []
    while (y, m) >= (start.year, start.month):
        out.append(f"{y:04d}{m:02d}")
        m -= 1
        if not m:
            y, m = y - 1, 12
    return out

def val(item, *keys):
    for key in keys:
        node = item.find(key)
        if node is not None and node.text:
            return node.text.strip()
    return ""

def won(s):
    try: return int(s.replace(",", "").strip() or 0) * 10000
    except ValueError: return 0

def money(n):
    e, rem = divmod(n, 100000000)
    m = rem // 10000
    if e and m: return f"{e}억 {m:,}만 원"
    if e: return f"{e}억 원"
    return f"{m:,}만 원"

def fetch_page(base, apartment, deal_ym, key, page):
    query = urllib.parse.urlencode({
        "serviceKey": key, "LAWD_CD": apartment["lawd_code"],
        "DEAL_YMD": deal_ym, "pageNo": str(page), "numOfRows": "1000",
    })
    req = urllib.request.Request(base + "?" + query,
        headers={"User-Agent": "ApartmentReport/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            root = ET.fromstring(response.read())
    except urllib.error.HTTPError as exc:
        try:
            error_root = ET.fromstring(exc.read())
            reason = error_root.findtext(".//returnReasonCode", default="")
            message = error_root.findtext(".//returnAuthMsg", default="")
        except ET.ParseError:
            reason, message = "", ""
        if reason == "30":
            raise RuntimeError("등록되지 않은 공공데이터 서비스 키입니다 (오류 코드 30). 해당 API 활용 신청과 발급 키를 확인하세요.") from exc
        if reason == "20":
            raise RuntimeError("이 API에 대한 접근 권한이 없습니다 (오류 코드 20). 공공데이터포털 활용 신청 및 승인 상태를 확인하세요.") from exc
        safe_message = message or "요청 권한 또는 API 상태를 확인하세요."
        raise RuntimeError(f"국토교통부 API HTTP {exc.code}: {safe_message}") from exc
    code = root.findtext(".//resultCode", default="00")
    if code == "03":
        return [], 0
    if code not in ("00", "000"):
        raise RuntimeError(root.findtext(".//resultMsg", default="국토교통부 API 오류"))
    try:
        total = int(root.findtext(".//totalCount", default="0"))
    except ValueError:
        total = 0
    return root.findall(".//item"), total

class FetchRows(list):
    """거래 행과 API별 조회 상태를 함께 보존하는 리스트 호환 결과."""
    def __init__(self, rows=(), api_status=None):
        super().__init__(rows)
        self.api_status = api_status or {}


def split_kakao_line(line, max_chars):
    """Split an unusually long transaction line without losing its text."""
    chunks = []
    line = str(line)
    while len(line) > max_chars:
        split_at = line.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(line[:split_at])
        line = line[split_at:].lstrip()
    if line:
        chunks.append(line)
    return chunks


def make_kakao_messages(results, queried_on=None, months=3, max_chars=200):
    """Build Kakao memo messages with sales first and each message <=200 chars."""
    queried_on = queried_on or date.today()
    start_date = lookback_start(months, queried_on)
    apartments = [(name, rows) for name, rows in results]
    include_name = len(apartments) > 1
    grouped = {"매매": [], "전세": [], "월세": []}
    for name, rows in apartments:
        for day, kind, detail in rows:
            if kind not in grouped:
                continue
            raw_day = str(day).strip()
            try:
                transaction_date = date.fromisoformat(raw_day)
            except ValueError:
                if len(raw_day) == 6 and raw_day.isdigit():
                    transaction_date = date(int(raw_day[:4]), int(raw_day[4:]), 1)
                else:
                    continue
            if not start_date <= transaction_date <= queried_on:
                continue
            grouped[kind].append((transaction_date, name, detail))

    counts = {kind: len(rows) for kind, rows in grouped.items()}
    total = sum(counts.values())
    header = f"총 거래 {total}건 | 매매 {counts['매매']} | 전세 {counts['전세']} | 월세 {counts['월세']}"
    messages = []
    current = [header]
    for kind in ("매매", "전세", "월세"):
        latest = sorted(grouped[kind], key=lambda item: item[0], reverse=True)
        if not latest:
            continue
        heading = f"[{kind}]"
        heading_added = False
        for transaction_date, name, detail in latest:
            parts = [part.strip() for part in detail.split("|")]
            area = parts[1].replace("㎡ (", "㎡(").strip() if len(parts) > 1 else "면적 미상"
            price = parts[2] if len(parts) > 2 else "금액 미상"
            price = price.replace("보증금 ", "").replace(" / 월 ", "/")
            prefix = f"{name} " if include_name else ""
            day_text = f"{transaction_date.month}월 {transaction_date.day}일"
            line = f"{prefix}{day_text} {area} {price}"
            chunks = split_kakao_line(line, max_chars - len(heading) - 1)
            if not heading_added:
                if len("\n".join(current + [heading, chunks[0]])) > max_chars:
                    messages.append("\n".join(current))
                    current = [heading]
                else:
                    current.append(heading)
                heading_added = True
            for chunk in chunks:
                if len("\n".join(current + [chunk])) <= max_chars:
                    current.append(chunk)
                else:
                    messages.append("\n".join(current))
                    current = [heading, chunk]
    if current:
        messages.append("\n".join(current))
    return messages


def fetch(apartment, months, key, today=None):
    rows = []
    aliases = apartment.get("complex_names") or [apartment["name"]]
    norm = lambda s: "".join(s.lower().split()).replace("아파트", "")
    alias_set = {norm(s) for s in aliases}
    seen_transactions = set()
    today = today or date.today()
    start_date = lookback_start(months, today)
    request_months = recent_months(months, today)
    api_counts = {"매매": 0, "전월세": 0}
    api_failures = {"매매": [], "전월세": []}
    for ym in request_months:
        for kind, base in (("매매", TRADE), ("전월세", RENT)):
            try:
                items, total = fetch_page(base, apartment, ym, key, 1)
                for page in range(2, ((total + 999) // 1000) + 1):
                    more, _ = fetch_page(base, apartment, ym, key, page)
                    items.extend(more)
            except Exception as exc:
                api_failures[kind].append((ym, str(exc)))
                marker = f"{kind} 오류"
                if not any(row[1] == marker for row in rows):
                    rows.append(("", marker, str(exc)))
                continue
            for item in items:
                name = val(item, "아파트", "aptNm", "apt")
                if norm(name) not in alias_set:
                    continue
                signature = (kind,) + tuple(val(item, *keys) for keys in (
                    ("aptSeq",), ("jibun",), ("dealYear", "년"), ("dealMonth", "월"), ("dealDay", "일"),
                    ("dealAmount", "거래금액", "deposit", "보증금액"), ("monthlyRent", "월세금액"),
                    ("excluUseAr", "전용면적"), ("floor", "층"), ("contractType",), ("contractTerm",),
                    ("preDeposit",), ("preMonthlyRent",)))
                if signature in seen_transactions:
                    continue
                seen_transactions.add(signature)
                y, m, d = val(item, "년", "dealYear"), val(item, "월", "dealMonth"), val(item, "일", "dealDay")
                try:
                    transaction_date = date(int(y), int(m), int(d))
                    if transaction_date < start_date or transaction_date > today:
                        continue
                    day = transaction_date.isoformat()
                except ValueError:
                    day = ym
                api_counts[kind] += 1
                rent = won(val(item, "월세금액", "monthlyRent"))
                typ = kind if kind == "매매" else ("월세" if rent else "전세")
                try:
                    area_m2 = float(val(item, '전용면적', 'excluUseAr'))
                    area = f"{area_m2:g}㎡ ({area_m2 * 0.3025:.1f}평)"
                except ValueError:
                    area = "면적 미상"
                floor_value = val(item, "층", "floor")
                floor = f"{floor_value}층" if floor_value else "층 미상"
                amount = won(val(item, "거래금액", "dealAmount", "보증금액", "deposit"))
                price = f"보증금 {money(amount)} / 월 {money(rent)}" if typ == "월세" else money(amount)
                rows.append((day, typ, f"{day} | {area} | {price} | {floor}"))
    statuses = {}
    for kind in ("매매", "전월세"):
        failed = api_failures[kind]
        if failed and len(failed) == len(request_months):
            state = "failure"
        elif failed:
            state = "partial_failure"
        else:
            state = "success" if api_counts[kind] else "zero"
        statuses[kind] = {
            "state": state,
            "count": api_counts[kind],
            "failed_months": len(failed),
            "total_months": len(request_months),
            "error": failed[0][1] if failed else "",
        }
    return FetchRows(sorted(rows, reverse=True), statuses)

def _api_status_text(status):
    state = status.get("state")
    count = status.get("count", 0)
    if state == "zero":
        return "정상 · 일치 거래 0건"
    if state == "success":
        return f"정상 · 일치 거래 {count}건"
    failure = f"{status.get('failed_months', 0)}/{status.get('total_months', 0)}개월 실패"
    label = "전체 실패" if state == "failure" else "부분 실패"
    details = f" · 일치 거래 {count}건 수집" if count else " · 일치 거래 0건 수집"
    error = f" · {status['error']}" if status.get("error") else ""
    return f"{label} · {failure}{details}{error}"


def make_report(results, months, queried_on=None):
    queried_on = queried_on or date.today()
    lines = [f"아파트 실거래 요약 | 최근 {months}개월", f"조회 기준일: {queried_on.isoformat()}", ""]
    for name, rows in results:
        lines.append(f"■ {name}")
        statuses = getattr(rows, "api_status", {})
        if statuses:
            lines.append(f"매매 API: {_api_status_text(statuses['매매'])}")
            lines.append(f"전월세 API: {_api_status_text(statuses['전월세'])}")
        trade_error = next((r for r in rows if r[1] == "매매 오류"), None)
        rent_error = next((r for r in rows if r[1] == "전월세 오류"), None)
        for kind, source_error in (("매매", trade_error), ("전세", rent_error), ("월세", rent_error)):
            found = [r for r in rows if r[1] == kind]
            if source_error:
                count_text = f"부분 결과 {len(found)}건" if found else "조회 불가"
                if kind == "매매" or kind == "전세":
                    lines.append(f"{kind}: {count_text} ({source_error[2]})")
                else:
                    lines.append(f"{kind}: {count_text} (전월세 API 오류)")
            else:
                lines.append(f"{kind}: {len(found)}건")
            lines.extend(["  · " + r[2] for r in found[:10]] or ["  · 조회 결과 없음"])
        lines.append("")
    lines.append("출처: 국토교통부 실거래 신고 자료. 현재 매물 현황이 아닙니다.")
    return "\n".join(lines)

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("아파트 실거래 요약")
        self.root.geometry("1180x800")
        self.root.minsize(760, 560)
        self.cfg = read_config()
        self.results, self.busy = [], False
        self.result_months = self.cfg.get("lookback_months", 3)
        self.result_date = date.today()
        self.result_apartments = []
        self.query_error = ""
        self.completion_queue = queue.Queue()
        self.configure_styles()
        self.build()
        self.refresh()
        self.root.after(50, self._poll_completion_queue)
        if self.cfg.get("auto_search_on_start") and self.cfg.get("molit_service_key"):
            root.after(300, self.search)

    def configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(background="#f3f6fa")
        style.configure("App.TFrame", background="#f3f6fa")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Title.TLabel", background="#f3f6fa", foreground="#172b4d",
                        font=("맑은 고딕", 17, "bold"))
        style.configure("Subtitle.TLabel", background="#f3f6fa", foreground="#617087",
                        font=("맑은 고딕", 9))
        style.configure("Section.TLabel", background="#ffffff", foreground="#20324d",
                        font=("맑은 고딕", 11, "bold"))
        style.configure("Hint.TLabel", background="#f3f6fa", foreground="#64748b",
                        font=("맑은 고딕", 9))
        style.configure("TButton", font=("맑은 고딕", 9), padding=(10, 6))
        style.configure("Primary.TButton", font=("맑은 고딕", 9, "bold"),
                        foreground="#ffffff", background="#2563eb", padding=(14, 7))
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#94a3b8")])
        style.configure("TCheckbutton", background="#f3f6fa", foreground="#334155",
                        font=("맑은 고딕", 9))
        style.configure("TSpinbox", padding=5)
        style.configure("Treeview", font=("맑은 고딕", 9), rowheight=27,
                         background="#fbfcfe", fieldbackground="#fbfcfe", foreground="#29364a")
        style.configure("Treeview.Heading", font=("맑은 고딕", 9, "bold"), foreground="#20324d")
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#172b4d")])

    def build(self):
        shell = ttk.Frame(self.root, style="App.TFrame", padding=(18, 14, 18, 10))
        shell.pack(fill="both", expand=True)
        heading = ttk.Frame(shell, style="App.TFrame")
        heading.pack(fill="x", pady=(0, 12))
        ttk.Label(heading, text="아파트 실거래 요약", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="관심 단지의 매매·전세·월세 신고 내역을 한눈에 확인하세요.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        top = ttk.Frame(shell, style="App.TFrame")
        top.pack(fill="x", pady=(0, 12))
        primary = ttk.Frame(top, style="App.TFrame")
        primary.pack(fill="x", pady=(0, 7))
        ttk.Label(primary, text="조회 기간", style="Subtitle.TLabel").pack(side="left")
        self.months = tk.IntVar(value=self.cfg["lookback_months"])
        ttk.Spinbox(primary, from_=1, to=24, width=5, textvariable=self.months).pack(side="left", padx=(8, 4))
        ttk.Label(primary, text="개월", style="Subtitle.TLabel").pack(side="left", padx=(0, 14))
        self.button = ttk.Button(primary, text="지금 조회", style="Primary.TButton", command=self.search)
        self.button.pack(side="left")
        ttk.Button(primary, text="보고서 저장", command=self.save_report).pack(side="left", padx=(8, 0))
        tools = ttk.Frame(top, style="App.TFrame")
        tools.pack(fill="x")
        ttk.Button(tools, text="카카오 설정", command=self.configure_kakao).pack(side="left", padx=(7, 0))
        ttk.Button(tools, text="API 키 설정", command=self.configure_api_key).pack(side="left")
        self.send_button = ttk.Button(tools, text="카카오톡 전송", command=self.send_report)
        self.send_button.pack(side="left", padx=(7, 0))
        self.shutdown_var = tk.BooleanVar(value=self.cfg.get("shutdown_after_send", False))
        ttk.Checkbutton(tools, text="발송 후 컴퓨터 종료", variable=self.shutdown_var,
                        command=self.save_shutdown_preference).pack(side="left", padx=(12, 0))
        ttk.Button(tools, text="종료 취소", command=self.cancel_shutdown).pack(side="left", padx=(7, 0))
        split = ttk.Panedwindow(shell, orient="horizontal"); split.pack(fill="both", expand=True)
        left = ttk.Frame(split); right = ttk.Frame(split); split.add(left, weight=1); split.add(right, weight=4)
        left_card = ttk.Frame(left, style="Card.TFrame", padding=12)
        left_card.pack(fill="both", expand=True, padx=(0, 8))
        ttk.Label(left_card, text="관심 아파트", style="Section.TLabel").pack(anchor="w")
        ttk.Label(left_card, text="조회할 단지를 선택해 관리합니다.", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 8))
        list_wrap = ttk.Frame(left_card, style="Card.TFrame")
        list_wrap.pack(fill="both", expand=True)
        self.list = tk.Listbox(list_wrap, exportselection=False, font=("맑은 고딕", 10),
                               background="#f8fafc", foreground="#26364d", selectbackground="#dbeafe",
                               selectforeground="#172b4d", relief="flat", highlightthickness=1,
                               highlightbackground="#e2e8f0", activestyle="none", borderwidth=0)
        list_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=list_scroll.set)
        self.list.pack(side="left", fill="both", expand=True); list_scroll.pack(side="right", fill="y")
        ttk.Button(left_card, text="＋ 아파트 추가", command=self.add).pack(fill="x", pady=(10, 5))
        ttk.Button(left_card, text="선택 삭제", command=self.remove).pack(fill="x")
        right_card = ttk.Frame(right, style="Card.TFrame", padding=12)
        right_card.pack(fill="both", expand=True, padx=(8, 0))
        ttk.Label(right_card, text="실거래 보고서", style="Section.TLabel").pack(anchor="w")
        ttk.Label(right_card, text="최근 신고 거래와 유형별 건수를 함께 살펴보세요.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 8))
        self.summary = tk.StringVar(value="조회 전")
        ttk.Label(right_card, textvariable=self.summary, style="Subtitle.TLabel").pack(anchor="w", pady=(0, 7))
        display = ttk.Panedwindow(right_card, orient="vertical"); display.pack(fill="both", expand=True, pady=(0, 5))
        report_frame = ttk.Frame(display); chart_frame = ttk.Frame(display)
        display.add(report_frame, weight=4); display.add(chart_frame, weight=2)
        ttk.Label(report_frame, text="단지별 조회 상태", style="Section.TLabel").pack(anchor="w", pady=(2, 4))
        state_wrap = ttk.Frame(report_frame); state_wrap.pack(fill="x", pady=(0, 8))
        self.state_tree = ttk.Treeview(state_wrap, columns=("name", "trade", "rent", "detail"),
                                       show="headings", height=3, selectmode="browse")
        for col, title, width in (("name", "관심 단지", 170), ("trade", "매매", 105),
                                  ("rent", "전월세", 105), ("detail", "오류 내용", 280)):
            self.state_tree.heading(col, text=title)
            self.state_tree.column(col, width=width, minwidth=75 if col != "detail" else 120,
                                   stretch=(col in ("name", "detail")), anchor="w")
        self.state_tree.tag_configure("ok", foreground="#087443")
        self.state_tree.tag_configure("error", foreground="#b42318")
        self.state_tree.tag_configure("partial", foreground="#a15c00")
        state_scroll = ttk.Scrollbar(state_wrap, orient="vertical", command=self.state_tree.yview)
        self.state_tree.configure(yscrollcommand=state_scroll.set)
        self.state_tree.pack(side="left", fill="x", expand=True); state_scroll.pack(side="right", fill="y")
        ttk.Label(report_frame, text="조회된 거래", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        rows_wrap = ttk.Frame(report_frame); rows_wrap.pack(fill="both", expand=True)
        self.rows_tree = ttk.Treeview(rows_wrap, columns=("name", "date", "kind", "price", "area", "floor"),
                                      show="headings", selectmode="browse")
        columns = (("name", "관심 단지", 160), ("date", "거래일", 95), ("kind", "유형", 65),
                   ("price", "거래 금액", 210), ("area", "전용면적", 135), ("floor", "층", 65))
        for col, title, width in columns:
            self.rows_tree.heading(col, text=title)
            self.rows_tree.column(col, width=width, minwidth=60, stretch=(col in ("name", "price")), anchor="w")
        self.rows_tree.tag_configure("매매", foreground="#1d4ed8")
        self.rows_tree.tag_configure("전세", foreground="#087443")
        self.rows_tree.tag_configure("월세", foreground="#a15c00")
        rows_scroll = ttk.Scrollbar(rows_wrap, orient="vertical", command=self.rows_tree.yview)
        self.rows_tree.configure(yscrollcommand=rows_scroll.set)
        self.rows_tree.pack(side="left", fill="both", expand=True); rows_scroll.pack(side="right", fill="y")
        ttk.Label(chart_frame, text="월별 거래 유형별 건수", style="Section.TLabel").pack(anchor="w", pady=(6, 4))
        chart_area = ttk.Frame(chart_frame, style="Card.TFrame")
        chart_area.pack(fill="both", expand=True, pady=(4, 0))
        self.chart = tk.Canvas(chart_area, height=220, background="#fbfcfe", highlightthickness=1,
                               highlightbackground="#e2e8f0", xscrollincrement=52)
        self.chart.pack(fill="both", expand=True)
        self.chart_scroll = ttk.Scrollbar(chart_area, orient="horizontal", command=self.chart.xview)
        self.chart_scroll.pack(fill="x")
        self.chart.configure(xscrollcommand=self.chart_scroll.set)
        self.chart.bind("<Configure>", lambda _event: self.draw_chart())
        self.status = tk.StringVar(value="준비")
        statusbar = ttk.Frame(shell, style="App.TFrame")
        statusbar.pack(fill="x", pady=(9, 0))
        ttk.Label(statusbar, textvariable=self.status, style="Section.TLabel").pack(side="left")
        ttk.Label(statusbar, text="국토교통부 신고 자료 · 현재 매물 수는 제공하지 않습니다.",
                  style="Hint.TLabel").pack(side="right")
        self.render_results()

    def configure_api_key(self):
        key = simpledialog.askstring("국토교통부 API 키", "공공데이터포털 서비스 키:", show="*", parent=self.root)
        if key is None:
            return
        self.cfg["molit_service_key"] = key.strip()
        write_config(self.cfg)
        messagebox.showinfo("저장 완료", f"API 키를 {USER_CONFIG.name}에 저장했습니다.")

    def configure_kakao(self):
        current = self.cfg.get("kakao", {})
        rest_api_key = simpledialog.askstring("카카오 연결", "카카오디벨로퍼스 REST API 키를 입력하세요.",
            initialvalue=current.get("client_id", ""), parent=self.root)
        if rest_api_key is None:
            return
        client_secret = simpledialog.askstring("카카오 연결", "카카오디벨로퍼스 Client Secret을 입력하세요.\n"
            "Client Secret을 사용하지 않도록 설정한 앱이면 비워 두세요.",
            initialvalue=current.get("client_secret", ""), show="*", parent=self.root)
        if client_secret is None:
            return
        self.cfg["kakao"] = {**current, "client_id": rest_api_key.strip(),
                             "client_secret": client_secret.strip()}
        write_config(self.cfg)
        messagebox.showinfo("카카오 연결 준비", "카카오디벨로퍼스 앱 설정에 다음 항목을 등록했는지 확인하세요.\n\n"
            "리다이렉트 URI: http://127.0.0.1:8765/oauth\n"
            "동의 항목: talk_message\n"
            "제품 링크 도메인: https://developers.kakao.com\n\n"
            "확인을 누르면 카카오 로그인 창이 열립니다.")
        self.busy = True
        self.send_button.configure(state="disabled")
        threading.Thread(target=self._kakao_auth_worker,
                         args=(rest_api_key.strip(), client_secret.strip()), daemon=True).start()

    def _kakao_auth_worker(self, rest_api_key, client_secret):
        try:
            tokens, error = authorize_kakao(rest_api_key, client_secret), None
        except Exception as exc:
            error = str(exc)
            if "KOE004" in error:
                error = ("카카오 로그인이 꺼져 있습니다. 카카오디벨로퍼스에서 앱 > "
                         "카카오 로그인 > 사용 설정을 ON으로 바꾼 뒤 다시 연결하세요.")
            tokens = {}
        self.completion_queue.put(("kakao_auth", tokens, error))

    def save_shutdown_preference(self):
        self.cfg["shutdown_after_send"] = bool(self.shutdown_var.get())
        write_config(self.cfg)

    def _send_kakao_report(self):
        if not self.results:
            messagebox.showinfo("카카오톡 보내기", "먼저 거래를 조회하세요.")
            return
        if not (self.cfg.get("kakao", {}).get("refresh_token") or
                self.cfg.get("kakao", {}).get("access_token")):
            messagebox.showinfo("카카오 연결 필요", "먼저 '카카오 설정'을 눌러 카카오 계정을 연결하세요.")
            return
        try:
            messages = make_kakao_messages(
                self.results, queried_on=self.result_date, months=3)
        except MessageSendError as exc:
            messagebox.showerror("카카오 메시지 오류", str(exc))
            return
        prompt = (f"내 카카오톡 '나와의 채팅'으로 {len(messages)}건을 보냅니다.\n"
                  "계속할까요?")
        if not messagebox.askyesno("카카오톡 보내기", prompt):
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        self.status.set("카카오톡 전송 중…")
        kakao_snapshot = dict(self.cfg["kakao"])
        threading.Thread(target=self._kakao_send_worker,
                         args=(messages, kakao_snapshot), daemon=True).start()

    def _kakao_send_worker(self, messages, kakao_snapshot):
        try:
            result = send_kakao_memo(kakao_snapshot, messages,
                on_tokens=lambda tokens: self.completion_queue.put(("kakao_tokens", tokens)))
            error = None
        except Exception as exc:
            result, error = "", str(exc)
        self.completion_queue.put(("kakao_send", result, error))

    def _finish_kakao_send(self, result, error):
        self.busy = False
        self.send_button.configure(state="normal")
        if error:
            self.status.set("카카오톡 전송 실패")
            messagebox.showerror("카카오톡 전송 실패", error)
            return
        self.status.set("카카오톡 전송 완료")
        messagebox.showinfo("카카오톡 전송 완료",
                            f"나와의 채팅방에 {result} 메시지를 보냈습니다.")
        if self.shutdown_var.get():
            self.schedule_shutdown()

    def send_report(self):
        self._send_kakao_report()
        return
        if not self.results:
            messagebox.showinfo("발송할 보고서 없음", "먼저 실거래를 조회하세요.")
            return
        if self.cfg.get("notification", {}).get("provider") != "solapi_lms":
            messagebox.showinfo("발송 설정 필요", "LMS 설정 버튼에서 SOLAPI 계정과 번호를 등록하세요.")
            return
        try:
            chunks = split_lms_text(make_report(self.results, self.result_months, self.result_date))
        except MessageSendError as exc:
            messagebox.showerror("보고서 오류", str(exc))
            return
        ending = "발송 접수 후 컴퓨터가 종료됩니다." if self.shutdown_var.get() else "발송 후 프로그램은 대기 상태로 남습니다."
        prompt = f"LMS {len(chunks)}건을 발송합니다. 발송 서비스 요금이 발생할 수 있습니다.\n{ending}\n계속할까요?"
        if not messagebox.askyesno("LMS 발송 확인", prompt):
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        self.status.set("LMS 발송 중…")
        solapi_snapshot = dict(self.cfg["solapi"])
        threading.Thread(target=self._send_worker, args=(chunks, solapi_snapshot), daemon=True).start()

    def _send_worker(self, chunks, solapi_snapshot):
        try:
            result = send_lms(**solapi_snapshot, chunks=chunks)
            error = None
        except Exception as exc:
            result, error = "", str(exc)
        self.completion_queue.put(("send", result, error))

    def _poll_completion_queue(self):
        try:
            while True:
                event = self.completion_queue.get_nowait()
                if event[0] == "search":
                    _, n, queried_on, apartments, data, err = event
                    self.done(n, queried_on, apartments, data, err)
                elif event[0] == "progress":
                    _, current, total, apartment_name = event
                    progress = f"단지 {current}/{total} 조회 중: {apartment_name}"
                    self.status.set(progress)
                    self.summary.set(f"최근 {self.result_months}개월 · {progress}")
                elif event[0] == "send":
                    _, group_id, error = event
                    self._finish_send(group_id, error)
                elif event[0] == "kakao_tokens":
                    self.cfg["kakao"] = event[1]
                    write_config(self.cfg)
                elif event[0] == "kakao_auth":
                    _, tokens, error = event
                    self.busy = False
                    self.send_button.configure(state="normal")
                    if error:
                        self.status.set("카카오 연결 실패")
                        messagebox.showerror("카카오 연결 실패", error)
                    else:
                        self.cfg["kakao"].update(tokens)
                        self.cfg.setdefault("notification", {})["provider"] = "kakao_me"
                        write_config(self.cfg)
                        self.status.set("카카오 연결 완료")
                        messagebox.showinfo("카카오 연결 완료", "이제 '카카오톡 전송'을 누르면 나와의 채팅방으로 거래 요약을 보냅니다.")
                elif event[0] == "kakao_send":
                    _, result, error = event
                    self._finish_kakao_send(result, error)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(50, self._poll_completion_queue)

    def _finish_send(self, group_id, error):
        self.busy = False
        self.send_button.configure(state="normal")
        if error:
            self.status.set("발송 실패")
            messagebox.showerror("LMS 발송 실패", error + "\n종료하지 않고 대기합니다.")
            return
        self.status.set("LMS 발송 접수 완료")
        messagebox.showinfo("발송 완료", f"발송 서비스가 접수했습니다. 그룹 ID: {group_id or '확인 불가'}")
        if self.shutdown_var.get():
            self.schedule_shutdown()

    def schedule_shutdown(self):
        try:
            subprocess.run(["shutdown.exe", "/s", "/t", "60"], check=True, capture_output=True)
            messagebox.showinfo("종료 예약", "60초 후 종료됩니다. 취소하려면 앱의 '종료 취소'를 누르세요.")
        except (OSError, subprocess.CalledProcessError) as exc:
            messagebox.showerror("종료 예약 실패", str(exc))

    def cancel_shutdown(self):
        try:
            subprocess.run(["shutdown.exe", "/a"], check=True, capture_output=True)
            messagebox.showinfo("종료 취소", "컴퓨터 종료 예약을 취소했습니다.")
        except subprocess.CalledProcessError:
            messagebox.showinfo("종료 예약 없음", "취소할 컴퓨터 종료 예약이 없습니다.")
        except OSError as exc:
            messagebox.showerror("오류", str(exc))

    def refresh(self):
        self.list.delete(0, "end")
        for apt in self.cfg["apartments"]: self.list.insert("end", f"{apt['name']} ({apt['lawd_code']})")

    def add(self):
        name = simpledialog.askstring("아파트 등록", "단지명:", parent=self.root)
        if not name: return
        code = simpledialog.askstring("아파트 등록", "법정동 시군구 코드 5자리:", parent=self.root)
        if not code: return
        code = code.strip()
        if len(code) != 5 or not code.isdigit():
            messagebox.showerror("입력 오류", "숫자 5자리를 입력하세요."); return
        alias = simpledialog.askstring("단지 별칭", "API 응답에 쓰이는 별칭 (없으면 취소):", parent=self.root)
        self.cfg["apartments"].append({"name": name.strip(), "lawd_code": code,
            "complex_names": [name.strip()] + ([alias.strip()] if alias and alias.strip() else [])})
        write_config(self.cfg); self.refresh()

    def remove(self):
        sel = self.list.curselection()
        if sel:
            del self.cfg["apartments"][sel[0]]; write_config(self.cfg); self.refresh()

    def search(self):
        if self.busy: return
        key = self.cfg.get("molit_service_key", "").strip()
        if not key:
            messagebox.showinfo("API 키 필요", "공공데이터포털에서 국토교통부 매매·전월세 API를 신청한 뒤 API 키 설정 버튼으로 키를 등록하세요.")
            return
        if not self.cfg["apartments"]:
            messagebox.showinfo("아파트 등록", "먼저 관심 아파트를 등록하세요."); return
        try:
            n = int(self.months.get())
            if not 1 <= n <= 24: raise ValueError()
        except (ValueError, tk.TclError):
            messagebox.showerror("입력 오류", "조회 개월은 1~24 사이 숫자여야 합니다."); return
        n = max(3, n)
        self.months.set(n)
        self.cfg["lookback_months"] = n; write_config(self.cfg)
        queried_on = date.today()
        apartments_snapshot = tuple(
            MappingProxyType({**apt, "complex_names": tuple(apt.get("complex_names", []))})
            for apt in self.cfg["apartments"])
        key_snapshot = str(key)
        self.result_months = n
        self.result_date = queried_on
        self.result_apartments = apartments_snapshot
        self.query_error = ""
        self.busy = True; self.button.configure(state="disabled"); self.status.set("조회 중…")
        self.summary.set(f"최근 {n}개월 · 조회 중")
        threading.Thread(target=self.worker,
                         args=(n, key_snapshot, apartments_snapshot, queried_on), daemon=True).start()

    def worker(self, n, key, apartments, queried_on):
        try:
            data = []
            total = len(apartments)
            for index, apartment in enumerate(apartments, start=1):
                self.completion_queue.put(("progress", index, total, apartment["name"]))
                data.append((apartment["name"], fetch(apartment, n, key, queried_on)))
            err = None
        except Exception as exc:
            data, err = [], str(exc)
        self.completion_queue.put(("search", n, queried_on, apartments, data, err))

    def done(self, n, queried_on, apartments, data, err):
        self.busy = False; self.button.configure(state="normal")
        self.result_months = n
        self.result_date = queried_on
        self.result_apartments = apartments
        if err:
            self.results = []
            self.query_error = err
            self.render_results()
            self.draw_chart()
            self.summary.set(f"조회 실패 · 기준일 {queried_on.isoformat()}")
            self.status.set("조회 실패"); messagebox.showerror("조회 오류", err); return
        self.results = data
        self.render_results()
        self.draw_chart()
        statuses = [status for _, rows in data for status in getattr(rows, "api_status", {}).values()]
        failed_count = sum(1 for status in statuses if status["state"] in ("failure", "partial_failure"))
        all_failed = bool(statuses) and all(status["state"] == "failure" for status in statuses)
        no_transactions = not any(r[1] in ("매매", "전세", "월세") for _, rows in data for r in rows)
        tx_count = sum(1 for _, rows in data for r in rows if r[1] in ("매매", "전세", "월세"))
        if all_failed:
            result_state = "전체 API 조회 실패"
        elif failed_count:
            result_state = f"부분 API 실패 {failed_count}건"
        elif no_transactions:
            result_state = "정상 조회 · 일치 거래 0건"
        else:
            result_state = "정상 조회"
        self.summary.set(f"최근 {n}개월 · 거래 {tx_count}건 · {result_state} · 기준일 {queried_on.isoformat()}")
        self.status.set(result_state + " · " + queried_on.isoformat())

    def render_results(self):
        self.state_tree.delete(*self.state_tree.get_children())
        self.rows_tree.delete(*self.rows_tree.get_children())
        if not self.results:
            if self.query_error:
                for apt in self.result_apartments:
                    self.state_tree.insert("", "end", values=(
                        apt["name"], "조회 실패", "조회 실패", self.query_error), tags=("error",))
                return
            for apt in self.cfg.get("apartments", []):
                self.state_tree.insert("", "end", values=(apt["name"], "대기", "대기", "조회 후 상태가 표시됩니다"))
            if not self.cfg.get("apartments"):
                self.summary.set("관심 아파트를 등록하고 조회하세요")
            elif self.summary.get() == "조회 전":
                self.summary.set("조회 후 단지별 성공·오류와 거래 내역이 표시됩니다")
            return
        for name, rows in self.results:
            trade_err = next((r[2] for r in rows if r[1] == "매매 오류"), None)
            rent_err = next((r[2] for r in rows if r[1] == "전월세 오류"), None)
            statuses = getattr(rows, "api_status", {})
            trade_count = sum(1 for r in rows if r[1] == "매매")
            jeonse_count = sum(1 for r in rows if r[1] == "전세")
            monthly_count = sum(1 for r in rows if r[1] == "월세")
            if statuses:
                trade_status = _api_status_text(statuses["매매"])
                rent_status = _api_status_text(statuses["전월세"])
            else:
                trade_status = f"오류 · {trade_count}건 포함" if trade_err and trade_count else ("조회 불가" if trade_err else f"정상 · {trade_count}건")
                rent_status = f"오류 · {jeonse_count + monthly_count}건 포함" if rent_err and jeonse_count + monthly_count else ("조회 불가" if rent_err else f"정상 · 전세 {jeonse_count} / 월세 {monthly_count}건")
            errors = [f"매매: {trade_err}" for _ in [0] if trade_err] + [f"전월세: {rent_err}" for _ in [0] if rent_err]
            tag = "error" if errors and not (trade_count + jeonse_count + monthly_count) else ("partial" if errors else "ok")
            self.state_tree.insert("", "end", values=(name, trade_status, rent_status, " | ".join(errors)), tags=(tag,))
            for day, kind, detail in rows:
                if kind not in ("매매", "전세", "월세"):
                    continue
                parts = [part.strip() for part in detail.split("|")]
                area = parts[1] if len(parts) > 1 else "면적 미상"
                price = parts[2] if len(parts) > 2 else "금액 미상"
                floor = parts[3] if len(parts) > 3 else "층 미상"
                self.rows_tree.insert("", "end", values=(name, day, kind, price, area, floor), tags=(kind,))

    def draw_chart(self):
        if not hasattr(self, "chart"):
            return
        canvas = self.chart
        canvas.delete("all")
        view_width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 180)
        if not self.results:
            canvas.configure(scrollregion=(0, 0, view_width, height))
            canvas.create_text(view_width / 2, height / 2, text="조회 후 월별 거래 건수를 그래프로 보여드립니다.",
                               fill="#687386", font=("맑은 고딕", 10))
            return

        kinds = (("매매", "#2563eb"), ("전세", "#059669"), ("월세", "#d97706"))
        kinds_by_name = {kind for kind, _ in kinds}
        try:
            period_months = max(1, min(24, int(self.result_months)))
        except (ValueError, tk.TclError):
            period_months = 3
        timeline = list(reversed(recent_months(period_months, self.result_date)))
        monthly_counts = {ym: {kind: 0 for kind, _ in kinds} for ym in timeline}
        for _name, rows in self.results:
            for row in rows:
                if len(row) < 2 or row[1] not in kinds_by_name:
                    continue
                raw_date = str(row[0]).strip()
                if len(raw_date) >= 7 and raw_date[4:5] == "-":
                    ym = raw_date[:7].replace("-", "")
                elif len(raw_date) >= 6 and raw_date[:6].isdigit():
                    ym = raw_date[:6]
                else:
                    continue
                if ym in monthly_counts:
                    monthly_counts[ym][row[1]] += 1
        counts = [[monthly_counts[ym][kind] for kind, _ in kinds] for ym in timeline]
        totals = [sum(monthly_counts[ym][kind] for ym in timeline) for kind, _ in kinds]
        max_count = max([count for group in counts for count in group] + totals, default=0)
        if max_count == 0:
            canvas.configure(scrollregion=(0, 0, view_width, height))
            canvas.create_text(view_width / 2, height / 2, text="조회 기간에 표시할 거래가 없습니다.",
                               fill="#687386", font=("맑은 고딕", 10))
            return

        left, right, top, bottom = 48, 18, 34, 44
        month_step = max(52, (view_width - left - right) / max(1, len(timeline) + 1))
        plot_w = max(view_width - left - right, (len(timeline) + 1) * month_step)
        content_width = left + plot_w + right
        canvas.configure(scrollregion=(0, 0, content_width, height))
        plot_h = max(40, height - top - bottom)
        for tick in range(4):
            value = round(max_count * tick / 3)
            y = top + plot_h * (1 - tick / 3)
            canvas.create_line(left, y, content_width - right, y, fill="#e8ebef")
            canvas.create_text(left - 8, y, text=str(value), anchor="e",
                               fill="#687386", font=("맑은 고딕", 8))

        bar_w = 10
        for i, (ym, group) in enumerate(zip(timeline, counts)):
            center = left + month_step * (i + 0.5)
            for j, ((kind, color), count) in enumerate(zip(kinds, group)):
                x = center + (j - 1) * (bar_w + 3)
                bar_h = plot_h * count / max_count
                y = top + plot_h - bar_h
                canvas.create_rectangle(x - bar_w / 2, y, x + bar_w / 2, top + plot_h,
                                        fill=color, outline="")
                if count:
                    canvas.create_text(x, y - 9, text=str(count), fill="#333",
                                       font=("맑은 고딕", 8))
            label = f"{ym[2:4]}.{ym[4:6]}"
            canvas.create_text(center, top + plot_h + 17, text=label, fill="#414957",
                               font=("맑은 고딕", 8))

        total_center = left + month_step * (len(timeline) + 0.5)
        canvas.create_line(total_center - month_step / 2, top - 3,
                           total_center - month_step / 2, top + plot_h + 5,
                           fill="#94a3b8", dash=(3, 3))
        for j, ((kind, color), count) in enumerate(zip(kinds, totals)):
            x = total_center + (j - 1) * (bar_w + 3)
            bar_h = plot_h * count / max_count
            y = top + plot_h - bar_h
            canvas.create_rectangle(x - bar_w / 2, y, x + bar_w / 2, top + plot_h,
                                    fill=color, outline="")
            canvas.create_text(x, y - 9, text=str(count), fill="#172b4d",
                               font=("맑은 고딕", 8, "bold"))
        canvas.create_text(total_center, top + plot_h + 17, text="총계", fill="#172b4d",
                           font=("맑은 고딕", 9, "bold"))

        legend_x = left
        for kind, color in kinds:
            canvas.create_rectangle(legend_x, 10, legend_x + 10, 20, fill=color, outline="")
            canvas.create_text(legend_x + 15, 15, text=kind, anchor="w",
                               fill="#414957", font=("맑은 고딕", 8))
            legend_x += 70

    def save_report(self):
        if not self.results:
            messagebox.showinfo("안내", "먼저 조회하세요."); return
        folder = ROOT / "reports"; folder.mkdir(exist_ok=True)
        path = folder / f"실거래요약_{date.today().isoformat()}.txt"
        path.write_text(make_report(self.results, self.result_months, self.result_date), encoding="utf-8")
        messagebox.showinfo("저장 완료", str(path))

def main():
    root = tk.Tk()
    try: App(root)
    except Exception as exc:
        messagebox.showerror("시작 오류", str(exc)); root.destroy(); return
    root.mainloop()

if __name__ == "__main__":
    main()
