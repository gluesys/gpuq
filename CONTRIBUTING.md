# 공동 개발

## 시작

```bash
git clone git@github.com:gluesys/gpuq.git
cd gpuq
git switch -c fix/issue-description
python3 -m unittest discover -s tests -v
bash -n gpuqueue-start smoke.sh
```

Python 표준 라이브러리만 사용한다. 테스트는 임시 디렉터리와 가짜 backend를 사용하고,
서버 접속이나 실제 NVMe 접근 없이 lease·취소·복구·정리 순서를 검증한다.

## 변경과 검토

Issue에 재현 명령, 예상 동작, 실제 동작, 로그와 관련 버전을 기록한다.
수정은 작업 브랜치에 커밋하고 Pull Request로 검토한다. PR에는 변경한 동작과 수행한 검증을 적는다.
자격 증명, SSH 키, 컨테이너 이미지와 장비의 원본 실행 로그는 커밋하지 않는다.

슬롯·lease·시간 제한·실패 후 정리의 동작이 바뀌면 해당 시나리오를 재현하는 테스트도 갱신한다.
문서나 낮은 영향의 편집만 있는 변경에 별도 테스트를 추가할 필요는 없다.

## 장비 검증

`deploy.py`와 `provision_test.py`는 원격 장비를 변경한다. `run_nvme_e2e.py`는 실제 연구용
NVMe를 반복 초기화한다. 이 도구들은 로컬 자동 테스트와 GitHub Actions에서 실행하지 않는다.
장비 검증 시에는 사용 중인 작업을 확인하고 공유 큐를 잠근 뒤, 검증한 커밋과 결과를 PR에 남긴다.
기존 설정 파일은 배포 시 덮어쓰지 않으므로 설정 변경은 별도로 반영한다.

## 다음 개발 대상

[사용자 직접 운영 설계](docs/user-managed-daos.md)를 기준으로 논의한다.
현재의 고정 이미지 구현과 예정된 동작을 문서에서 구분하며, 아직 없는 CLI 옵션을 사용 가능한 것으로 안내하지 않는다.
