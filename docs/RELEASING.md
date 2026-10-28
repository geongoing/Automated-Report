# 정식 버전 배포

프로그램은 공개 저장소 `geongoing/Automated-Report`의 정식 Releases만 업데이트 출처로 사용합니다.
사용자 로그인이나 GitHub 토큰을 EXE에 포함하지 않습니다.

## 새 버전

1. 업무 처리와 업데이트 단위 검증을 통과시킵니다.
2. `local-web/version.json`의 버전·기준일과 사용자 안내·인수인계를 갱신합니다.
3. `docs/releases/v버전.md`에 사용자가 이해할 변경 내용을 작성합니다.
4. 변경을 커밋해 `main`에 올린 후 **같은 버전의 태그**를 게시합니다.

```sh
git tag v2.2.0
git push origin v2.2.0
```

태그는 예시입니다. 실제 버전 정보와 정확히 일치해야 합니다.
GitHub Actions의 **Windows release**가 단위 검증, Windows EXE 빌드, 독립 EXE 검증을 수행합니다.
모두 통과한 경우에만 초안 Release에 파일을 올린 뒤 정식 버전으로 게시합니다.
웹의 Actions에서 수동으로 실행할 수도 있지만 같은 버전의 Release가 이미 있으면 실패합니다.

## 배포 파일

- `SongdoDaily-v버전.zip`: 처음 사용하는 사람에게 전달. `일지작성.exe` 하나를 포함합니다.
- `SongdoDaily.exe`: 자동 업데이트용 실행 파일.
- `update.json`: 정식 버전·크기·SHA-256·플랫폼 정보.
- `SHA256SUMS.txt`: 배포 파일의 SHA-256 목록.

업데이트 조회는 `/releases/latest/download/update.json`, 실행 파일은 해당 버전의 `/releases/download/v버전/SongdoDaily.exe`를 사용합니다.
임의 주소, 테스트 버전, 인증 정보나 업무 자료를 배포 파일에 추가하지 않습니다.
자동 업데이트는 새 파일의 검증과 서버 시작을 확인한 후에 활성화합니다. 실패한 동일 파일을 다음 실행마다 반복 설치하지 않습니다.

## 배포 수정

공개한 버전의 태그나 EXE를 교체하지 않습니다. 수정한 버전 번호로 새 Release를 게시합니다.
일지 생성 중에는 서버가 종료 요청을 거부합니다. 업데이트는 다음 실행으로 미뤄 업무 파일 쓰기를 보호합니다.
클라우드 소스 검사만으로 Windows 동작 확인을 대신하지 않습니다. Windows release의 독립 EXE 검증 결과를 확인합니다.
