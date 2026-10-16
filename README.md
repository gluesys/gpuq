# gpuq

SSH로 접속한 연구자들이 GPU 한 장과 DAOS 서버 네 대를 순서대로 사용하는 작업 큐다.
Task Spooler가 실행 순서를 관리하고 Python broker가 DAOS 작업과 종료 후 정리를 담당한다.

## 공동 개발 기준

이 저장소의 초기 코드는 2026-10-16 cxl2와 da1~da4에 설치된 구현을 기준으로 한다.
주 실행 코드와 네 서버 helper의 SHA-256이 설치본과 일치함을 확인했다.

공개본의 장비 주소·디스크 시리얼·설정의 이미지 ID는 예제 값으로 바꿨다.
기존 환경의 실제 설정과 인증 정보는 포함하지 않는다. `activate_nvme.py`의 기존 장비
시리얼도 예제이므로 다른 장비에 적용하기 전에 해당 장비의 allowlist와 맞춰야 한다.

**현재 구현은 기관 프로필에 고정된 이미지로 DAOS를 먼저 시작한 뒤 사용자 스크립트를 실행한다.**
각 사용자가 root 권한으로 자기 슬롯 안에서 원하는 이미지를 배포하고 컨테이너를 직접 실행하는
방식은 다음 개발 대상이며 아직 구현되지 않았다. 목표와 검증 조건은
[사용자 직접 운영 설계](docs/user-managed-daos.md)에 정리했다.

| 파일 | 역할 |
|---|---|
| `gpuq` | 제출·조회·로그·취소 CLI |
| `gpuqueue-start`, `gpuqueue-wait-exec` | 공유 큐 시작과 GPU·정리 완료 대기 |
| `gpuqueue_daos.py` | lease, 시간 제한, 사용자 작업, 네 서버 정리 관리 |
| `gpuqueue_node.py` | 서버별 DAOS 실행과 지정된 NVMe 초기화 |
| `systemd/` | 큐 및 broker 서비스 정의 |
| `config/` | cxl2·da1~da4 설정; 배포 기본값은 `enabled=false` |
| `tests/` | 실제 저장장치를 사용하지 않는 자동 테스트 |
| `deploy.py`, `activate_nvme.py` | 기존 연구환경 배포와 최초 NVMe 전환 |
| `run_*e2e.py`, `provision_test.py`, `smoke.sh` | 연구장비 통합 검증 도구 |

로컬 개발은 Linux와 Python 3.8 이상에서 외부 Python 패키지 없이 시작할 수 있다.

```bash
python3 -m unittest discover -s tests -v
bash -n gpuqueue-start smoke.sh
```

GitHub Actions는 구문 검사와 위 자동 테스트만 실행한다. 연구장비에 접속하거나 NVMe를 초기화하지 않는다.
운영 환경은 Rocky Linux, Task Spooler, systemd, Podman, SSH, NVIDIA 드라이버와 DAOS 이미지를 사용한다.
`deploy.py`는 공유 큐와 `gpuqueue` 계정·그룹이 이미 준비된 환경을 전제로 한다.
`systemd/gpuqueue.service`와 `gpuqueue-start`에는 현재 큐의 시작 구성을 함께 보관했다.

- 변경 방법: [CONTRIBUTING.md](CONTRIBUTING.md)
- 이번 공유 시점 검증: [docs/validation-2026-10-16.md](docs/validation-2026-10-16.md)
- 과거 장비 검증: [RESULTS.md](RESULTS.md)
- 기존 사용 가이드: [INSTITUTION_GUIDE.md](INSTITUTION_GUIDE.md)

`config/`에는 주소·인터페이스·디스크 시리얼과 이미지 ID의 예제가 들어 있다.
배포 전에 실제 장비의 구성과 allowlist를 맞춘다. 이미지 파일, 실행 로그, 로컬 인증 정보는 Git에 포함하지 않는다.

## 현재 구현 사용법

`gpuq submit --inst inst1|inst2` 작업에 DAOS 서버 4대와 cxl2 GPU를 함께 할당한다.
풀은 미리 만들지 않는다. 작업자가 작업 안에서 직접 생성한다.

## 사용

```bash
gpuq submit --inst inst1 --time 4h -- bash experiment.sh
# 위 명령이 출력한 실제 작업 번호를 아래 JOB_ID에 사용
gpuq list
gpuq log JOB_ID
gpuq follow JOB_ID
gpuq cancel JOB_ID
gpuq daos-status
```

`gpuq`만 실행하면 목록을 표시한다. `--inst` 없는 기존 GPU 전용 작업도 지원한다.
`--inst` 작업은 서버 준비/실험에 시간 제한을 적용하며, 그 뒤 정리는 별도로 완료한다.
기존 셸의 기관별 `DAOS_POOL`/`DAOS_CONTAINER`는 새 작업에 넘기지 않는다.

실험 스크립트 안에서는 서버와 같은 버전의 이미지에 있는 클라이언트를 사용한다.

```bash
gpuq daos-exec -- bash -euc '
  dmg -o "$DAOS_CONTROL_CONFIG" system query
  dmg -o "$DAOS_CONTROL_CONFIG" pool create --size=32G --nsvc=1 --nranks=4 my-pool
  daos -G "$DAOS_SYSTEM" container create --type=POSIX my-pool my-data
  # 실제 실험 수행
'
```

이미지는 서버/agent/클라이언트를 함께 포함한다. `daos-exec`는 현재 작업의 사용자 UID로
실행되며, 제출한 작업 디렉터리를 같은 경로에 마운트한다. 추가 경로가 필요한 실험은
운영자와 마운트 정책을 먼저 정한다. 호스트 Python venv가 이미지 안에 자동으로 생기지
않으므로, 특정 Python 모듈을 쓸 때는 해당 모듈을 이미지에 포함해야 한다.
GPU/GDS를 컨테이너 내부에서 사용하는 설정은 별도이며 현재 `daos-exec` 범위에 포함하지 않는다.

물리 NVMe 서버 이미지는 `kmod`/`pciutils`를 포함해야 한다. DAOS 자동 준비가
SPDK setup 스크립트에서 `modprobe`/`lsmod`를 호출한다. 물리 모드에서 `/dev`를 호스트와
공유하여 PCI 재연결 시 새로 생성되는 NVMe/UIO 장치 노드를 볼 수 있도록 한다.
파일 기반 테스트 컨테이너에는 이 호스트 `/dev` 마운트를 제공하지 않는다.

## 유지보수 잠금

```bash
sudo gpuq lock --reason 'DAOS 유지보수'
gpuq list
gpuq daos-status
sudo gpuq unlock
```

잠금 중 일반 사용자의 새 제출은 안내 메시지와 함께 거부된다. 이미 제출된 일반 사용자
작업도 실행 게이트에서 대기한다. root 검증 작업만 실행할 수 있다. 실행 중인 작업은
강제 종료하지 않으므로 유지보수 전에 종료를 확인한다. 조회·로그·취소는 계속 가능하다.
잠금 파일은 `/etc/gpuqueue-daos/maintenance.json`이며 서비스 재시작 후에도 유지된다.
DAOS 정리 실패의 recovery hold와 별개이므로 `unlock`만으로 복구 hold를 해제할 수 없다.

## 수명주기와 정리

1. GPU 유휴 및 이전 DAOS lease 정리 완료 대기.
2. 네 노드 preflight: 기존 서버/engine과 겹치면 실행 거부. 기존 서비스를 임의로 내리지 않음.
3. root 소유 설정에 고정된 이미지로 네 서버 컨테이너 시작.
4. 새 DAOS 시스템 포맷, rank 4개 joined 확인. 풀 자동 생성 없음.
5. 별도 systemd cgroup에서 제출자 UID로 실험 실행.
6. 정상/실패/timeout/cancel/클라이언트 강제 종료에 대해 root broker가 정리 수행.
7. 실험 cgroup 종료 → agent 종료 → 네 서버 전부 종료 확인 → 연구용 NVMe discard 및 메타데이터 제거.
8. 네 노드 정리 성공 후에만 다음 작업 실행 가능.

**물리 NVMe 모드는 해당 작업의 풀·컨테이너·데이터를 전부 삭제한다.** `blkdiscard`는
전체 네임스페이스 논리 초기화이며 보안 삭제가 아니다. 지원하지 않는 장치는 실패로 처리하고
다른 파괴 명령으로 자동 대체하지 않는다. 시리얼/PCI ID 불일치, 파티션/마운트/holder/open FD,
외부 engine, 외부 lease가 있으면 삭제를 거부한다.

컨테이너를 멈추는 것만으로 이전 NVMe/관리 DB를 다음 버전에서 재사용하지 않는다.
작업마다 새 메타데이터 경로를 쓰고 종료 시 NVMe와 해당 경로를 함께 초기화한다.
서버별 allowlist는 `config/da1.json`~`da4.json`이며 OS 디스크나 HDD는 선택하지 않는다.

정리 실패/제어 서버 강제 종료/재부팅 시 영구 lease를 남기고 모든 새 GPU 작업을 보류한다.
task-spooler가 취소 작업을 finished로 표시해도 broker가 정리 중이면 다음 작업 명령은 시작되지 않는다.
로그를 확인하고 관리자가 아래 명령으로 같은 lease의 정리를 재시도한다.

```bash
sudo journalctl -u gpuqueue-daos -n 100
sudo /usr/local/libexec/gpuqueue-daos recover
```

`blocked.json` 또는 `active.json`을 수동 삭제해서 우회하지 않는다. 네 노드 중 하나라도
접속/종료/초기화 실패 시 hold를 유지한다. 제어 서비스가 내려가 있어도 GPU 작업을 시작하지 않는다.

## 배포 및 활성화

```bash
python3 -m unittest discover -s tests -v
python3 deploy.py
```

배포 스크립트는 기존 설정 파일을 덮어쓰지 않고 새 설정은 **disabled**로 설치한다.
task-spooler를 재시작하지 않는다. 원본 gpuq/wait-exec는 cxl2의
`/var/lib/gpuqueue-daos/backups/<배포시각>/`에 보관한다.

실제 NVMe 활성화에는 기존 데이터 삭제 확인, 이전 시스템 중지/자동 시작 해제,
네 노드의 allowlist 점검, 이미지 배포 및 각 config의 enabled 전환이 필요하다.
기존 환경 데이터 삭제를 승인받기 전에는 이 절차를 수행하지 않는다.

운영자 전환 도구는 기본 실행 시 계획만 표시한다.

```bash
sudo /usr/local/sbin/gpuqueue-daos-activate
# 기존 데이터 삭제가 승인된 유지보수 시간에만 실행:
sudo /usr/local/sbin/gpuqueue-daos-activate --execute --destroy-existing-data
```

전환은 GPU 큐 유휴 확인 → 영구 hold → 기존 두 시스템 정상 종료 및 자동 시작 해제 →
네 서버의 시리얼/사용 여부 전부 확인 → 전체 초기화 → 새 모드 활성화 순서다.
중간 실패 시 hold를 유지한다. `activating` 단계에서는 일반 recover로 초기화를 우회할 수
없고 전환 도구를 다시 실행해 네 서버의 claim을 모두 완료해야 한다.
기존 `/etc/daos/inst*.env`는 이전 시스템용이므로 전환 후 새 작업은 `--inst`와
`gpuq daos-exec`를 사용한다. 실행 중 작업에는 새 시스템/agent/control config를 자동 전달한다.

활성화 후 유지보수 잠금 안에서 실제 NVMe 전체 경로를 반복 검증할 때:

```bash
python3 run_nvme_e2e.py
```

이 검증도 실제 풀·데이터를 만들고 삭제한다. 네 서버의 초기화 감사 기록, 시리얼,
파일시스템 signature 없음, 메타데이터 제거와 lease 반환까지 확인한다.

이미지 빌드 예:

```bash
podman build --build-arg DAOS_RPM_REPO_URL="$DAOS_RPM_REPO_URL" \
  --build-arg DAOS_RPM_VERSION="$DAOS_RPM_VERSION" \
  -t localhost/gpuq-daos:2.8.1-nvme .
podman image inspect localhost/gpuq-daos:2.8.1-nvme --format '{{.Id}}'
```

`DAOS_RPM_REPO_URL`과 `DAOS_RPM_VERSION`은 접근 가능한 EL8용 DAOS RPM 저장소와
해당 패키지 버전으로 먼저 설정한다. 저장소 URL은 이미지 빌드에 필요하며 공개본에 내부 주소를 담지 않았다.

각 기관 profile은 서버/클라이언트 이미지의 로컬 `sha256:` ID에 고정한다.
태그만 바꿔서 실행 중인 작업의 버전을 바꾸지 않는다. 새 이미지는 모든 서버와 cxl2에
배포한 뒤 대기 중인 작업이 없을 때 설정을 변경한다. 기존 DAOS·컨테이너 커널은 호스트와 공유한다.

## 로그 및 테스트 범위

- 사용자 작업: `gpuq log JOB_ID`, `/mnt/nvme1/gpuqueue/logs/<UID>/ts-out.*`
- 제어 서비스: `journalctl -u gpuqueue-daos`
- cxl2 lease 기록: `/var/lib/gpuqueue-daos/<lease>.json`
- 서버 로그/초기화 기록: `/var/lib/gpuqueue-daos/jobs/<lease>/`
- 비파괴 통합 테스트: `--test` 노드 helper + 별도 포트/임시 파일 bdev.
  `/var/lib/gpuqueue-daos-test/`만 제거하며 기존 NVMe/metadata를 건드리지 않는다.

테스트 모드도 호스트와 커널 및 프로세스 목록을 공유한다. **기존 호스트 DAOS의
재시작/`nvme prepare`와 테스트 컨테이너 실행을 동시에 하지 않는다.** 호스트의 준비
명령이 컨테이너의 `daos_server`를 감지해 거부할 수 있다. GPU 작업 종료 뒤 외부에서
수행하는 서버 재시작까지 GPU 큐가 자동으로 조율하지는 않으므로 테스트 시간을 조율해야 한다.
실제 NVMe 운영 모드는 기존 호스트 서비스를 해제하고 한 lifecycle만 실행하는 구성이다.

테스트 결과와 실제 활성화 여부는 `RESULTS.md`를 참고한다.
