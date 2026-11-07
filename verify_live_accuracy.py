"""Compare app output to an independent live pull of the MOLIT APIs.

Run with the bundled runtime: python verify_live_accuracy.py
The report compares transaction display rows after exact duplicate records
have been collapsed; it does not measure the accuracy of MOLIT source data.
"""
from collections import Counter
from datetime import date
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import app


def text(item, *names):
    for name in names:
        node = item.find(name)
        if node is not None and node.text:
            return node.text.strip()
    return ""


def amount(raw):
    try:
        return int(raw.replace(",", "").strip() or 0) * 10_000
    except ValueError:
        return 0


def money(won):
    eok, rest = divmod(won, 100_000_000)
    man = rest // 10_000
    if eok and man:
        return f"{eok}억 {man:,}만 원"
    if eok:
        return f"{eok}억 원"
    return f"{man:,}만 원"


def direct_page(base, apt, ym, key, page):
    params = urllib.parse.urlencode({
        "serviceKey": key,
        "LAWD_CD": apt["lawd_code"],
        "DEAL_YMD": ym,
        "pageNo": str(page),
        "numOfRows": "1000",
    })
    req = urllib.request.Request(base + "?" + params, headers={"User-Agent": "ApartmentAccuracyAudit/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        root = ET.fromstring(response.read())
    code = root.findtext(".//resultCode", default="00")
    if code == "03":
        return [], 0
    if code not in ("00", "000"):
        raise RuntimeError(root.findtext(".//resultMsg", default=f"API code {code}"))
    total = int(root.findtext(".//totalCount", default="0"))
    return root.findall(".//item"), total


def oracle_rows(apt, months, key):
    aliases = apt.get("complex_names") or [apt["name"]]
    normalize = lambda s: "".join(s.lower().split()).replace("아파트", "")
    aliases = {normalize(name) for name in aliases}
    start = app.lookback_start(months)
    today = date.today()
    unique = {}
    errors = []

    for kind, base in (("매매", app.TRADE), ("전월세", app.RENT)):
        for ym in app.recent_months(months):
            try:
                items, total = direct_page(base, apt, ym, key, 1)
                for page in range(2, (total + 999) // 1000 + 1):
                    more, _ = direct_page(base, apt, ym, key, page)
                    items.extend(more)
            except Exception as exc:
                errors.append(f"{kind}: {exc}")
                break

            for item in items:
                name = text(item, "aptNm", "아파트", "apt")
                if normalize(name) not in aliases:
                    continue
                year = text(item, "dealYear", "년")
                month = text(item, "dealMonth", "월")
                day_num = text(item, "dealDay", "일")
                try:
                    deal_date = date(int(year), int(month), int(day_num))
                except ValueError:
                    continue
                if not start <= deal_date <= today:
                    continue

                monthly = amount(text(item, "monthlyRent", "월세금액"))
                category = kind if kind == "매매" else ("월세" if monthly else "전세")
                raw_amount = text(item, "dealAmount", "거래금액") if kind == "매매" else text(item, "deposit", "보증금액")
                deposit = amount(raw_amount)
                area = text(item, "excluUseAr", "전용면적")
                try:
                    area_display = f"{float(area):g}㎡"
                except ValueError:
                    area_display = "면적 미상"
                floor = text(item, "floor", "층") or "층 미상"
                price = f"보증금 {money(deposit)} / 월 {money(monthly)}" if category == "월세" else money(deposit)
                line = f"{deal_date.isoformat()} | {area_display} | {price} | {floor}"

                signature = (
                    category,
                    text(item, "aptSeq"), text(item, "jibun"), deal_date.isoformat(),
                    raw_amount, text(item, "monthlyRent", "월세금액"), area, floor,
                    text(item, "contractType"), text(item, "contractTerm"),
                    text(item, "preDeposit"), text(item, "preMonthlyRent"),
                )
                unique[signature] = (category, line)

    return Counter(value for value in unique.values()), errors


def main():
    cfg = app.read_config()
    months = int(cfg["lookback_months"])
    key = cfg.get("molit_service_key", "")
    expected = Counter()
    observed = Counter()
    errors = []
    apartments = cfg.get("apartments", [])

    for apt in apartments:
        oracle, api_errors = oracle_rows(apt, months, key)
        errors.extend(f"{apt['name']} {error}" for error in api_errors)
        expected.update((apt["name"], *row) for row, count in oracle.items() for _ in range(count))
        app_rows = app.fetch(apt, months, key)
        observed.update((apt["name"], row[1], row[2]) for row in app_rows if row[1] in ("매매", "전세", "월세"))

    matched = sum((expected & observed).values())
    expected_count, observed_count = sum(expected.values()), sum(observed.values())
    precision = matched / observed_count if observed_count else (1.0 if not expected_count else 0.0)
    recall = matched / expected_count if expected_count else (1.0 if not observed_count else 0.0)
    agreement = matched / (matched + sum((observed - expected).values()) + sum((expected - observed).values())) if (expected_count or observed_count) else 1.0

    print(json.dumps({
        "window_start": app.lookback_start(months).isoformat(),
        "window_end": date.today().isoformat(),
        "expected_unique_rows": expected_count,
        "app_rows": observed_count,
        "matched_rows": matched,
        "precision_percent": round(precision * 100, 2),
        "recall_percent": round(recall * 100, 2),
        "agreement_percent": round(agreement * 100, 2),
        "api_errors": errors,
    }, ensure_ascii=False, indent=2))

    missing = expected - observed
    extra = observed - expected
    if missing:
        print("원본에는 있고 앱 결과에 없는 행:")
        for row, count in missing.items():
            print(count, row)
    if extra:
        print("앱 결과에는 있고 원본 비교에서 발견되지 않은 행:")
        for row, count in extra.items():
            print(count, row)
    if errors or min(precision, recall, agreement) < 0.98:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
