# 🐱 집사 고양이

PC에 사는 고양이가 유튜브 쇼츠·릴스 같은 업무 방해 요소를 감지해 막아주는 생산성 앱. (v0.1 개발 중)

## 무엇을 기록하나 (개인정보)
- 지금 보고 있는 **앱 이름**, **창 제목**, 브라우저의 **사이트 주소(호스트만, 예: `youtube.com`)**, 사용 시간
- 기록은 **이 PC에만** 저장된다: `%LOCALAPPDATA%\cat-app\cat.db`
- 인터넷으로 아무것도 보내지 않는다. 기록을 지우려면 위 파일을 삭제하면 된다.

## 설치 (Windows, Python 3.10+)
```powershell
git clone <이 저장소 주소>
cd <폴더>
pip install -r requirements.txt   # 브라우저 주소 읽기용 (없으면 쇼츠 구분 불가)
```

## 실행
```powershell
python cat_app.py --simulate      # 가짜 시나리오로 동작 확인 (아무 OS)
python cat_app.py                 # 고양이 창이 뜨고 감시 모드로 시작
python cat_app.py 업무모드         # 업무모드(쇼츠·릴스 탭 차단)로 시작
```
켜져 있는 동안 고양이 창에서 **감시 모드 ↔ 업무모드**를 언제든 바꿀 수 있다. 창을 닫으면 종료되고 오늘 통계가 터미널에 나온다.
```powershell
python cat_app.py --report        # 오늘 가장 많이 쓴 앱 TOP 5
```

> 한 번에 하나만 실행된다. 이미 켜져 있으면 두 번째 실행은 안내 메시지와 함께 종료된다.

## 테스트
```powershell
python test_rules.py   # 규칙 판정 (AND/OR 그룹, 우선순위)
python test_app.py     # SQLite 저장/조회
```

## 구조
| 파일 | 역할 |
|---|---|
| `watch.py` | 창 감지(Windows), 규칙 판정, 세션 집계 — 스파이크에서 검증된 로직 |
| `cat_app.py` | 규칙을 SQLite에서 읽고 기록을 SQLite에 저장하는 앱 본체 |
| `설계/sqlite(db설계도).md` | DB 설계도 (ERD, 설계 이유) |
| `cat-spike/README.md` | 초기 검증(스파이크) 체크리스트 |
| `진행도.md` | 개발 진행 기록 |

## 라이선스
MIT
