import json
import tempfile
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

import app
import messaging


class AppDataTests(unittest.TestCase):
    def test_fetch_reports_trade_and_rent_api_states_independently(self):
        trade_item = ET.fromstring("<item><aptNm>한빛</aptNm><dealYear>2025</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><dealAmount>50000</dealAmount><excluUseAr>59</excluUseAr></item>")
        def fake_page(base, apartment, ym, key, page):
            if base == app.TRADE:
                return [trade_item], 1
            raise RuntimeError("임시 전월세 장애")
        with patch.object(app, "recent_months", return_value=["202501"]), \
             patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), \
             patch.object(app, "fetch_page", side_effect=fake_page):
            rows = app.fetch({"name": "한빛", "lawd_code": "11110"}, 1, "test")
        self.assertEqual(rows.api_status["매매"]["state"], "success")
        self.assertEqual(rows.api_status["매매"]["count"], 1)
        self.assertEqual(rows.api_status["전월세"]["state"], "failure")
        report = app.make_report([("한빛", rows)], 1, app.date(2025, 1, 2))
        self.assertIn("매매 API: 정상 · 일치 거래 1건", report)
        self.assertIn("전월세 API: 전체 실패 · 1/1개월 실패", report)

    def test_fetch_distinguishes_zero_partial_failure_and_total_failure(self):
        def run(months, failures):
            def fake_page(base, apartment, ym, key, page):
                if base == app.TRADE and ym in failures:
                    raise RuntimeError("모의 오류")
                return [], 0
            with patch.object(app, "recent_months", return_value=months), \
                 patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), \
                 patch.object(app, "fetch_page", side_effect=fake_page):
                return app.fetch({"name": "한빛", "lawd_code": "11110"}, 1, "test")
        zero = run(["202501"], set())
        partial = run(["202501", "202412"], {"202501"})
        failed = run(["202501", "202412"], {"202501", "202412"})
        self.assertEqual(zero.api_status["매매"]["state"], "zero")
        self.assertEqual(partial.api_status["매매"]["state"], "partial_failure")
        self.assertEqual((partial.api_status["매매"]["failed_months"], partial.api_status["매매"]["total_months"]), (1, 2))
        self.assertEqual(failed.api_status["매매"]["state"], "failure")

    def test_worker_uses_supplied_snapshot_and_only_posts_queue_events(self):
        apartment = app.MappingProxyType({"name": "한빛", "lawd_code": "11110", "complex_names": ("한빛",)})
        event_queue = queue.Queue()
        fake_app = app.App.__new__(app.App)
        fake_app.completion_queue = event_queue
        call_info = []
        def fake_fetch(apt, months, key, queried_on):
            call_info.append((apt, months, key, queried_on, threading.current_thread()))
            return app.FetchRows()
        queried_on = app.date(2025, 1, 15)
        with patch.object(app, "fetch", side_effect=fake_fetch):
            worker = threading.Thread(target=app.App.worker,
                                      args=(fake_app, 4, "snapshot-key", (apartment,), queried_on))
            worker.start()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(call_info), 1)
        apt, months, key, day, worker_thread = call_info[0]
        self.assertIs(apt, apartment)
        self.assertEqual((months, key, day), (4, "snapshot-key", queried_on))
        self.assertNotEqual(worker_thread, threading.current_thread())
        with self.assertRaises(TypeError):
            apt["name"] = "변경"
        self.assertEqual(event_queue.get_nowait(), ("progress", 1, 1, "한빛"))
        event = event_queue.get_nowait()
        self.assertEqual(event[0:4], ("search", 4, queried_on, (apartment,)))
        self.assertIsNone(event[-1])

    def test_recent_months_rolls_over_year(self):
        class FakeDate:
            def __init__(self, year, month, day):
                self.year, self.month, self.day = year, month, day
            @classmethod
            def today(cls):
                return cls(2025, 1, 15)
        with patch.object(app, "date", FakeDate):
            self.assertEqual(app.recent_months(3), ["202501", "202412", "202411", "202410"])
        self.assertEqual(app.lookback_start(3, app.date(2025, 1, 15)), app.date(2024, 10, 15))

    def test_money_formats_won(self):
        self.assertEqual(app.won("12,345"), 123_450_000)
        self.assertEqual(app.money(123_450_000), "1억 2,345만 원")
        self.assertEqual(app.won("invalid"), 0)

    def test_config_separates_user_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path, user_path = root / "settings.conf", root / "user_settings.yaml"
            cfg = {**app.DEFAULT, "lookback_months": 6, "molit_service_key": "secret",
                   "apartments": [{"name": "한빛", "lawd_code": "11110"}],
                   "solapi": {"api_key": "key", "api_secret": "secret"}}
            with patch.object(app, "CONFIG", cfg_path), patch.object(app, "USER_CONFIG", user_path):
                app.write_config(cfg)
                saved_app = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertNotIn("molit_service_key", saved_app)
                self.assertNotIn("apartments", saved_app)
                loaded = app.read_config()
            self.assertEqual(loaded["lookback_months"], 6)
            self.assertEqual(loaded["molit_service_key"], "secret")
            self.assertEqual(loaded["apartments"][0]["name"], "한빛")
            self.assertEqual(loaded["solapi"]["api_key"], "key")

    def test_fetch_page_parses_xml_and_pagination_count(self):
        payload = b'<response><header><resultCode>00</resultCode></header><body><totalCount>2</totalCount><items><item><aptNm>A</aptNm></item></items></body></response>'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return payload
        with patch.object(app.urllib.request, "urlopen", return_value=Response()) as open_url:
            items, total = app.fetch_page(app.TRADE, {"lawd_code": "11110"}, "202501", "k", 1)
        self.assertEqual(total, 2)
        self.assertEqual(items[0].findtext("aptNm"), "A")
        self.assertIn("pageNo=1", open_url.call_args.args[0].full_url)

    def test_fetch_page_reports_unregistered_service_key_without_url(self):
        from urllib.error import HTTPError
        import io
        error_xml = b'<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg><returnAuthMsg>registered key error</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>'
        error = HTTPError("https://apis.data.go.kr/path?serviceKey=secret", 403, "Forbidden", {}, io.BytesIO(error_xml))
        with patch.object(app.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "오류 코드 30") as caught:
                app.fetch_page(app.TRADE, {"lawd_code": "11110"}, "202501", "secret", 1)
        self.assertNotIn("secret", str(caught.exception))
    def test_fetch_page_treats_documented_no_data_as_empty(self):
        payload = b'<response><header><resultCode>03</resultCode><resultMsg>No Data</resultMsg></header><body /></response>'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return payload
        with patch.object(app.urllib.request, "urlopen", return_value=Response()):
            items, total = app.fetch_page(app.RENT, {"lawd_code": "11110"}, "202501", "k", 1)
        self.assertEqual(items, [])
        self.assertEqual(total, 0)

    def test_rent_deposit_uses_documented_deposit_field(self):
        rent_item = ET.fromstring("<item><아파트>한빛</아파트><년>2025</년><월>1</월><일>4</일><deposit>30000</deposit><monthlyRent>0</monthlyRent><excluUseAr>59.9</excluUseAr></item>")
        def fake_page(base, apartment, ym, key, page):
            return ([rent_item] if base == app.RENT else [], 1 if base == app.RENT else 0)
        with patch.object(app, "recent_months", return_value=["202501"]), patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), patch.object(app, "fetch_page", side_effect=fake_page):
            rows = app.fetch({"name": "한빛", "lawd_code": "11110"}, 1, "k")
        lease = next(row for row in rows if row[1] == "전세")
        self.assertIn("3억 원", lease[2])
    def test_fetch_filters_alias_and_classifies_rent(self):
        def item(name, day, rent="0", amount="50000"):
            return ET.fromstring(f"<item><아파트>{name}</아파트><년>2025</년><월>1</월><일>{day}</일><월세금액>{rent}</월세금액><거래금액>{amount}</거래금액><전용면적>59.9</전용면적><층>10</층></item>")
        trade = item("한빛아파트", "2")
        lease = item("한빛아파트", "4", "80")
        def fake_page(base, apartment, ym, key, page):
            return ([trade] if base == app.TRADE else [lease], 1)
        with patch.object(app, "recent_months", return_value=["202501"]), patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), patch.object(app, "fetch_page", side_effect=fake_page):
            rows = app.fetch({"name": "한빛", "lawd_code": "11110", "complex_names": ["한빛아파트"]}, 1, "k")
        self.assertEqual([r[1] for r in rows], ["월세", "매매"])
        self.assertTrue(all("2025-01" in r[2] for r in rows))

    def test_fetch_filters_transactions_before_rolling_window(self):
        old = ET.fromstring("<item><aptNm>한빛</aptNm><dealYear>2024</dealYear><dealMonth>12</dealMonth><dealDay>31</dealDay><deposit>30000</deposit><monthlyRent>0</monthlyRent><excluUseAr>59.9</excluUseAr><floor>10</floor></item>")
        recent = ET.fromstring("<item><aptNm>한빛</aptNm><dealYear>2025</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><deposit>31000</deposit><monthlyRent>0</monthlyRent><excluUseAr>59.9</excluUseAr><floor>10</floor></item>")
        def fake_page(base, apartment, ym, key, page):
            return ([old, recent] if base == app.RENT else [], 2 if base == app.RENT else 0)
        with patch.object(app, "recent_months", return_value=["202501"]), patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), patch.object(app, "date") as fake_date, patch.object(app, "fetch_page", side_effect=fake_page):
            actual_date = __import__('datetime').date
            fake_date.today.return_value = actual_date(2025, 1, 15)
            fake_date.side_effect = actual_date
            rows = app.fetch({"name": "한빛", "lawd_code": "11110"}, 1, "k")
        self.assertEqual(len(rows), 1)
        self.assertIn("2025-01-02", rows[0][2])

    def test_fetch_deduplicates_identical_api_transactions(self):
        duplicate = ET.fromstring("<item><aptNm>한빛</aptNm><jibun>80</jibun><dealYear>2025</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><deposit>30000</deposit><monthlyRent>0</monthlyRent><excluUseAr>59.9</excluUseAr><floor>10</floor><contractType>신규</contractType></item>")
        def fake_page(base, apartment, ym, key, page):
            return ([duplicate, duplicate] if base == app.RENT else [], 2 if base == app.RENT else 0)
        with patch.object(app, "recent_months", return_value=["202501"]), patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), patch.object(app, "fetch_page", side_effect=fake_page):
            rows = app.fetch({"name": "한빛", "lawd_code": "11110"}, 1, "k")
        self.assertEqual(len(rows), 1)
    def test_fetch_keeps_rent_results_when_trade_service_is_unavailable(self):
        rent_item = ET.fromstring("<item><aptNm>한빛</aptNm><dealYear>2025</dealYear><dealMonth>1</dealMonth><dealDay>2</dealDay><deposit>30000</deposit><monthlyRent>0</monthlyRent><excluUseAr>59.9</excluUseAr><floor>10</floor></item>")
        def fake_page(base, apartment, ym, key, page):
            if base == app.TRADE:
                raise RuntimeError("매매 API 미승인")
            return [rent_item], 1
        with patch.object(app, "recent_months", return_value=["202501"]), patch.object(app, "lookback_start", return_value=app.date(2025, 1, 1)), patch.object(app, "fetch_page", side_effect=fake_page):
            rows = app.fetch({"name": "한빛", "lawd_code": "11110"}, 1, "k")
        report = app.make_report([("한빛", rows)], 1)
        self.assertIn("매매: 조회 불가 (매매 API 미승인)", report)
        self.assertIn("전세: 1건", report)
        self.assertLess(report.index("매매:"), report.index("전세:"))
        self.assertLess(report.index("전세:"), report.index("월세:"))

    def test_report_counts_and_disclaims_current_listing(self):
        report = app.make_report([("한빛", [("2025-01-02", "매매", "거래")])], 3)
        self.assertIn("매매: 1건", report)
        self.assertIn("전세: 0건", report)
        self.assertIn("현재 매물 현황이 아닙니다", report)


class MessagingTests(unittest.TestCase):
    def test_split_respects_utf8_byte_limit(self):
        source = ("아파트 거래 요약입니다.\n" * 180) + "끝"
        chunks = messaging.split_lms_text(source, max_bytes=240)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(c.encode("utf-8")) <= 240 for c in chunks))
        self.assertIn("끝", "".join(chunks))

    def test_send_lms_builds_request_and_returns_group(self):
        payload = {"groupInfo": {"count": {"registeredSuccess": 1}, "groupId": "g1"}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(payload).encode()
        with patch.object(messaging.urllib.request, "urlopen", return_value=Response()) as open_url:
            result = messaging.send_lms("key", "secret", "010-1234-5678", "010-9999-0000", ["보고서"])
        req = open_url.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(result, "g1")
        self.assertEqual(body["messages"][0]["to"], "01099990000")
        self.assertTrue(req.headers["Authorization"].startswith("HMAC-SHA256 apiKey=key"))

    def test_send_lms_rejects_partial_acceptance(self):
        payload = {"groupInfo": {"count": {"registeredSuccess": 0}, "groupId": "g1"},
                   "failedMessageList": [{"statusCode": "E1", "statusMessage": "failed"}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(payload).encode()
        with patch.object(messaging.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(messaging.MessageSendError):
                messaging.send_lms("key", "secret", "01012345678", "01099990000", ["보고서"])

    def test_send_lms_rejects_missing_credentials_without_network(self):
        with patch.object(messaging.urllib.request, "urlopen") as open_url:
            with self.assertRaises(messaging.MessageSendError):
                messaging.send_lms("", "secret", "01012345678", "01099990000", ["내용"])
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()




