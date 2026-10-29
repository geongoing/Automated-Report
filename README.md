# Automated Report

송도지소의 당일 레포트와 전날 최종 일지를 연결해 **최종 일지와 송도 HMI를 함께 생성**하는 Windows용 로컬 웹 프로그램입니다.
현재 버전은 **v2.6.0**입니다. 사용자 안내는 [사용방법.md](사용방법.md), 개발 인수인계는 [CLOUD_HANDOFF.md](CLOUD_HANDOFF.md)를 참고하세요.

배포본은 [최신 정식 버전](https://github.com/geongoing/Automated-Report/releases/latest)에서 받습니다.
`SongdoDaily-v버전.zip`에는 `일지작성.exe` 하나가 들어 있습니다. 이후 EXE 실행 시 새 정식 버전을 자동 확인합니다.
다운로드·검증·시작 확인이 실패하면 기존 버전을 사용하며 설정과 업무 자료를 보존합니다.

## 구성

- `local-web/report.py`: 일지 계산, 이월, XLSX 읽기·수정
- `local-web/hmi.py`: 같은 계산 결과로 HMI 생성
- `local-web/server.py`: HTTP API, 생성 중 종료 방지, 서버 수명 관리
- `local-web/app_settings.py`, `drafts.py`: 폴더 설정·선택, 날짜별 임시 저장
- `local-web/report_files.py`, `report_storage.py`: 자료 검색·이월, 중복 확인·일지 쌍 저장
- `local-web/folder_picker.py`: Windows 폴더 선택 창
- `local-web/runtime.py`, `app_launcher.py`, `launcher.pyw`: 사용자 데이터 경로, 콘솔 없는 실행, 서버 재사용
- `local-web/updater.py`: 정식 버전 확인, 검증된 실행 파일 설치, 이전 버전 복구
- `local-web/maintenance.py`: 사용 중인 프로그램을 보호하는 캐시 정리, 용량 제한 로그
- `local-web/persistence.py`: 파일 동기화·원자적 상태 저장, 정상 백업 3개와 자동 복원
- `local-web/transactions.py`: 완성된 XLSX 게시, 중단된 일지 쌍 저장의 재시작 복구
- `local-web/static/`: 웹 화면
- `local-web/assets/`: 메모지·연필 아이콘
- `송도_HMI.xlsx`: 빌드 및 HMI 생성용 양식 자산

실제 운영자료, 사용자 설정, 임시 입력값, 실행 기록, EXE와 버전별 ZIP은 Git에서 제외합니다.
설정과 임시 기록은 `%LOCALAPPDATA%\SongdoDaily`에 보관하며 EXE 옆에 업무 폴더를 만들지 않습니다.

## 소스 실행

Python 3.10 이상으로 실행합니다. 일반 실행에는 외부 Python 패키지가 필요하지 않습니다.

```sh
python -B local-web/server.py --no-browser --port 8765
```

`http://127.0.0.1:8765/`에 접속한 뒤 사용할 기존 폴더 3곳을 설정합니다.
클라우드에서는 웹 화면과 계산·파일 처리 코드를 개발할 수 있습니다. 실제 PC의 자료 경로는 클라우드에서 사용할 수 없으므로 별도의 테스트 폴더와 테스트 자료를 사용하세요.
Windows 폴더 선택과 EXE 실행은 Windows에서 최종 확인합니다.

개발용 설정을 분리하려면 실행 전에 `LOCALAPPDATA`를 별도의 테스트 폴더로 지정하세요.

## Windows EXE 빌드

검증한 빌드 환경은 Windows 64비트, Python 3.12와 PyInstaller 6.22.3입니다.
Windows에서 다음 명령을 실행합니다.

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r local-web\requirements-build.txt
.venv\Scripts\python.exe -B local-web\build_exe.py
```

루트의 `일지작성.exe`가 새로 만들어집니다. 기존 EXE 서버를 종료한 상태에서 빌드하세요.
빌드 임시 파일은 `%LOCALAPPDATA%\SongdoBuild`에 저장됩니다.
소스·화면을 수정한 뒤에는 EXE도 다시 빌드해야 배포본에 반영됩니다.

## 검증 및 배포

```sh
python -B -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_drafts_ui.js
```

테스트 XLSX는 `tests/fixtures.py`가 가상의 값으로 생성합니다. 실제 운영자료를 사용하지 않습니다.
이월·계산과 전체 HMI 매핑, 월말·연말·윤일의 자료 연결, 게시 직전·직후 중단 복구를 검증합니다. Node 검사는 브라우저 임시 저장의 입력 묶음 처리, 즉시 저장, 종료 전 전송과 실패 후 재시도를 확인합니다.
Windows 빌드 후 `python -B tests/frozen_smoke.py --exe dist/SongdoDaily.exe`로 독립 EXE, 생성·재작성, 설정 보존 및 실행 실패 복구를 확인합니다.
배포는 [배포 절차](docs/RELEASING.md)를 따릅니다. 버전 태그를 올리면 GitHub Actions의 Windows 빌드·검증을 거쳐 정식 Release가 게시됩니다.

## 간단한 소스 확인

다음 명령은 실제 업무 자료를 변경하거나 서버를 시작하지 않습니다.

```sh
python -B -c "import sys; sys.path.insert(0, 'local-web'); import report, hmi, server, folder_picker; print(server.APP_VERSION)"
```

업무 계산이나 생성 흐름을 수정할 때는 별도 폴더에 복사한 테스트 자료로 최종 일지·HMI의 값과 기존 파일 보존을 확인하세요.
