# 초기 공유본 검증

검증일: 2026-10-16. 로컬 환경: Linux, Python 3.8.10.

```text
python3 -m unittest discover -s tests -v
Ran 26 tests in 3.926s
OK
```

검사 범위는 정상 종료, 사용자 명령 실패, 시간 초과, 접속 종료, 부분 기동 실패,
동시 lease 거부, 복구 hold, daemon 재시작, 유지보수 잠금, 네 서버 오류 수집,
다른 lease·사용 중 장치 보호, NVMe 재연결 대기와 초기화 실패 처리다.
실제 저장장치나 운영 서비스는 이 자동 테스트에서 변경하지 않았다.

게시 준비 시 cxl2의 아래 네 실행 파일과 da1~da4의 node helper를 읽기 전용으로 확인했고,
모두 공개용 예제 값으로 치환하기 전 원본 코드와 SHA-256이 일치했다.
아래는 원본의 대조 기록이다. 공개본의 `activate_nvme.py`에는 장비 시리얼 대신 예제 값을 사용한다.

| 소스 | 설치 위치 | SHA-256 |
|---|---|---|
| `gpuq` | cxl2 `/usr/local/bin/gpuq` | `72b6e2dcd6039a8f38116524aa567e1fa0b6279ed47216066d49c1a6ec7f6fe4` |
| `gpuqueue-wait-exec` | cxl2 `/usr/local/libexec/gpuqueue-wait-exec` | `5419a3b9d9549336e11c9b645ce015be5375377e6c158e1ec5f26449e55fd38f` |
| `gpuqueue_daos.py` | cxl2 `/usr/local/libexec/gpuqueue-daos` | `f983a9c31014617b007fc44c61486f45edc96240951addeb32d7fd4384504124` |
| `activate_nvme.py` | cxl2 `/usr/local/sbin/gpuqueue-daos-activate` | `1e3395e775792a3444b37dfe761e1728763004b28a6a4d09bb66277b7800c319` |
| `gpuqueue_node.py` | da1~da4 `/usr/local/libexec/gpuqueue-daos-node` | `a58dab3f06ed9b02e1fdc2802c8534d69ad46dd5fa8dfe104c280f8f4d9ba02f` |

큐 서비스와 시작 스크립트도 cxl2 설치본에서 가져왔다.
이번 게시 준비에서는 서버 배포, 서비스 재시작, 장비 통합 테스트나 NVMe 초기화를 수행하지 않았다.
과거 실제 장비 검증의 요약은 [RESULTS.md](../RESULTS.md)에 있다.
