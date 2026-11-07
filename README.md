# 아파트 실거래 요약

등록한 아파트의 매매·전세·월세 실거래를 국토교통부 공공데이터 API에서 조회해 화면에 정리하는 Windows 앱입니다. 현재 매물 수는 조회하지 않습니다.

## 실행

Python 3.10 이상 설치 후 프로젝트 폴더에서 python run.py 실행. 먼저 pip install -r requirements.txt 로 YAML 라이브러리를 설치합니다.

## 사용자 설정

- user_settings.yaml: 국토교통부 API 키와 관심 아파트 목록
- settings.conf: 조회 기간과 앱 동작 옵션

앱의 API 키 설정 버튼으로 키를 입력할 수 있습니다. 아파트 추가 화면에서 단지명, 법정동 시군구 코드 5자리, 필요하면 API 응답 단지 별칭을 등록합니다.

user_settings.yaml 예시:

molit_service_key: "공공데이터포털 서비스 키"
apartments:
  - name: "관심 단지"
    lawd_code: "11290"
    complex_names:
      - "관심 단지"
      - "API 응답의 단지명"

공공데이터포털에서 다음 API의 활용 신청이 필요합니다.
- 아파트 매매: https://www.data.go.kr/data/15126469/openapi.do
- 아파트 전월세: https://www.data.go.kr/data/15126474/openapi.do

결과는 reports 폴더에 텍스트 파일로 저장할 수 있습니다. user_settings.yaml은 사용자 API 키를 포함하므로 Git에서 제외됩니다.

## Windows 로그인 시 자동 조회

Python이 설치되고 python run.py가 정상 실행되는 환경에서 PowerShell을 열어 아래 스크립트를 실행하면 현재 사용자 로그인 시 앱이 시작됩니다. 앱은 자동 조회 설정이 켜져 있고 API 키가 등록된 경우 조회를 시작합니다.

- 등록: .\install_startup.ps1
- 해제: .\uninstall_startup.ps1

이 스크립트는 Windows 작업 스케줄러의 현재 사용자 로그온 작업을 등록합니다.

## LMS 발송 및 종료

LMS에는 현재 SOLAPI 어댑터가 있습니다. 앱의 LMS 설정에서 API Key, API Secret, 사전 등록된 발신번호, 수신번호를 저장합니다. 계정이 설정되면 보고서 발송 버튼을 눌렀을 때 분할 건수와 요금 발생 가능성을 확인한 뒤 발송을 승인할 수 있습니다. SOLAPI 발송 문서는 HMAC-SHA256 인증과 LMS 메시지 API를 규정합니다.

발송 후 종료 체크박스는 기본 해제입니다. 켜고 발송을 확인한 경우에만 SOLAPI의 접수 결과가 성공일 때 60초 종료 예약을 요청합니다. 접수 실패 시 종료하지 않습니다. 종료 예약은 종료 취소 버튼으로 취소할 수 있습니다. 발송 서비스의 접수는 최종 수신 완료와 별개일 수 있으므로 업체 발송 내역에서 상태를 확인하세요.

알림톡 템플릿 연동은 아직 구현되지 않았습니다.

## 자동 검증

자동 회귀 테스트는 번들 Python에서 다음 명령으로 실행할 수 있습니다.

```powershell
python -m unittest discover -s tests -v
python -m py_compile app.py messaging.py run.py tests/test_app.py
```

테스트는 국토교통부 API와 SOLAPI를 모의 응답으로 검증하며 실제 조회·문자 발송은 하지 않습니다.

실제 API 원본과 앱 결과를 비교하려면 다음 명령을 실행합니다. 저장된 공공데이터 키와 네트워크 연결이 필요합니다.

```powershell
python verify_live_accuracy.py
```

이 검증은 조회 기간의 시작·종료일, 원본 고유 거래 행 수, precision/recall/전체 일치율을 출력합니다. 98% 미만이거나 API 오류가 있으면 종료 코드 1로 끝납니다. 출처 API 자체의 사실 정확도를 평가하는 검증은 아닙니다.
