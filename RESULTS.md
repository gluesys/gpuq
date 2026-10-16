# gpuq DAOS 수명주기 개발·검증 결과

> 아래는 당시 연구장비 검증 기록이다. 잠금·실행 상태는 현재 상태를 뜻하지 않는다.
> `results/`의 원본 로그와 감사 기록은 로컬 보관 자료이며 Git에는 포함하지 않는다.
> 공유본의 재실행 검증은 [검증 기록](docs/validation-2026-10-16.md)에 별도로 정리했다.

검증일: 2026-10-16, cxl2 / da1~da4.

## 실제 NVMe 전환 및 검증

사용자의 “gpuq 락 걸어 두고 나머지 진행” 지시에 따라 유지보수 잠금 후 실제 NVMe 모드로 전환했다.
기존 `da12_nvme`·`da34_nvme` 풀·데이터는 초기화했으며, 기존 서버/agent 서비스의 자동 시작을 해제했다.
새 모드는 작업 하나에 서버 네 대, 4 ranks/16 targets를 할당하고 작업마다 직접 풀을 생성한다.

- cxl2와 da1~da4의 운영 설정은 `enabled=true`다. 저장소 배포 기본값은 계속 `false`다.
- 유지보수 잠금은 **유지**한다. 일반 사용자 제출·직접 broker 실행·잠금 해제는 거부되고 기존 대기 작업도 실행 게이트에서 보류된다. 조회·로그·취소는 가능하며 root만 검증 작업을 실행한다.
- 잠금은 `/etc/gpuqueue-daos/maintenance.json`으로 유지되고 서비스 재시작 뒤에도 남는다. 사용자 개방 명령은 `sudo gpuq unlock`이다.
- 자동 테스트 **26개 통과**: [결과](results/nvme-activation-20261016/unit-tests.txt).
- 실제 NVMe의 반복 기동·새 시스템 포맷·작업 종료·discard를 검증했다. 각 lease마다 네 노드의 `blkdiscard` 감사 기록, 일치하는 시리얼, 빈 filesystem signature, 메타데이터/owner 제거를 확인했다.

실제 NVMe 검증은 유지보수 잠금 안에서 root 작업으로 수행했다. 제출자 UID와 일반 계정 권한 경로는 아래의 파일 bdev 검증 및 이번 유지보수 거부 검사로 별도 확인했다.

| 실제 NVMe 시나리오 | 작업 ID | 결과 |
|---|---:|---|
| inst1: 4 ranks, 32G 풀, POSIX 읽기/쓰기 체크섬, A6000 CUDA | 45 | exit 0 |
| inst2: 같은 검증 및 4대 전체 교대 | 46 | exit 0 |
| 실험 실패 후 자동 정리 | 47 | 예상 exit 23 |
| 시간 초과 후 자동 정리 | 48 | 예상 exit 124 |
| 취소 후 자동 정리 | 49 | 예상 exit 130 |
| da4 정리 실패 주입 | 50 | 예상 exit 75, hold |
| 다음 GPU 작업 보류 → root 복구 후 실행 | 51 | exit 0 |
| 제어 서비스 cgroup SIGKILL → 재시작 hold → root 복구 | 52 | 예상 exit 75, 복구 |

증거: [NVMe 시나리오 요약](results/nvme-activation-20261016/summary.json), [실행 기록](results/nvme-activation-20261016/e2e-run.log), [잠금 검사](results/nvme-activation-20261016/maintenance-tests.json), 같은 디렉터리의 lease별 `*-cleanup.json`.

최종 상태는 [네 서버 점검](results/nvme-activation-20261016/nodes-final.json), [cxl2 점검](results/nvme-activation-20261016/cxl2-final.json)으로 확인했다. 실행·대기 작업, 관리 컨테이너, GPU 프로세스, 활성 lease 및 recovery hold는 모두 없고 유지보수 잠금만 남아 있다. [설치 코드 SHA-256](results/nvme-activation-20261016/code-sha256.json)은 cxl2와 네 서버 모두 작업 폴더와 일치한다.

실제 장치 전환 중 수정한 사항:

1. NVMe 커널 재연결 직후 sysfs가 준비되기까지 기다린 후 시리얼을 검사하도록 보완했다. 비어 있는 시리얼을 일치로 간주하지 않으며 다른 시리얼은 거부한다.
2. 첫 물리 모드 기동에서 이미지에 `modprobe`/`lsmod`가 없어 SPDK 준비가 실패했다. 진단 작업 43·44는 취소하고 자동 정리했다. 이미지에 `kmod`·`pciutils`를 추가하여 수정했다.
3. 물리 서버 컨테이너는 호스트 `/dev`를 공유하여 PCI 재연결로 생성되는 NVMe/UIO 노드를 반영한다. 파일 테스트 모드에는 이 마운트를 제공하지 않는다. server/helper 로그도 lease별로 보관한다.

최종 이미지 ID (DAOS RPM 버전은 동일):

```text
sha256:87dc307431b8f7d942493acf32b8ec6d9638746227fff2b734bd7bdbcf55e94f
```

기관 안내: [INSTITUTION_GUIDE.md](INSTITUTION_GUIDE.md). cxl2에도 `/usr/local/share/doc/gpuqueue-daos/`에 설치하고 공개 smoke 스크립트는 `/mnt/nvme1/gpuqueue/examples/daos-smoke.sh`로 제공한다.

아래 내용은 이번 실제 NVMe 전환 **이전**에 수행한 비파괴 검증 기록이다.

## 구현 및 설치

- `gpuq submit --inst inst1|inst2`: GPU 대기 → 네 서버 preflight → DAOS 서버/agent 컨테이너 기동 → 새 시스템 포맷 → rank 4개 joined 확인 → 사용자 작업 → 정리.
- 풀은 사용자가 생성하며 미리 제공하지 않는다.
- 사용자 작업은 제출자 UID와 별도 systemd cgroup에서 실행한다. `gpuq daos-exec`는 같은 버전의 클라이언트를 사용한다.
- 정상·실패·시간 초과·취소·클라이언트 종료에 대해 root broker가 cgroup과 컨테이너를 정리한다.
- 서버 네 대의 종료를 모두 확인한 뒤 승인된 NVMe만 discard하고 해당 작업 메타데이터를 제거하는 코드를 구현했다.
- 정리 실패/제어 서비스 강제 종료 시 영구 hold. 새 작업은 복구가 완료될 때까지 시작하지 않는다.
- 프로세스 간 파일 잠금으로 전환 도구와 작업 실행을 조율한다. 서버 이미지·시스템명·포트가 기관 profile과 다르면 기동 전에 거부한다.
- 기존 `--inst` 없는 GPU 전용 작업과 로그 명령을 유지한다.

설치 위치: cxl2 `/usr/local/bin/gpuq`, `/usr/local/libexec/gpuqueue-daos`, `/usr/local/libexec/gpuqueue-wait-exec`, `/usr/local/sbin/gpuqueue-daos-activate`.
서버 helper: da1~da4 `/usr/local/libexec/gpuqueue-daos-node`.
원본 GPU 도구는 cxl2 `/var/lib/gpuqueue-daos/backups/`에 보관했다.

DAOS 이미지: `2.8.1-3.exastor.202610140412.gd245523a.el8`

```text
sha256:e8b1ef12f4932e62dd9bb8f7b4dbb7df33e1956292a739bf236bb5faeee63a9f
```

동일 이미지를 cxl2와 서버 네 대에 배포했다. 설치된 운영 코드와 작업 폴더의 SHA-256 일치는 [code-sha256.json](results/code-sha256.json)으로 확인했다.

## 테스트

자동 테스트 **24개 통과**: [unit-tests.txt](results/unit-tests.txt).
Python 구문 검사 및 `bash -n smoke.sh` 통과.

실제 서버 네 대에서는 기존 NVMe 대신 **노드당 16 GiB 파일 bdev, engine 1개/target 1개, SCM 4 GiB, 별도 포트**를 사용했다.
IB `ofi+verbs;ofi_rxm`으로 통신했고, 물리 NVMe는 테스트 컨테이너에서 숨겼다.

| 시나리오 | 최종 작업 ID | 결과 |
|---|---:|---|
| inst1: rank 4개, 사용자 풀 생성, POSIX 쓰기/읽기·SHA-256, A6000 CUDA | 33 | exit 0 |
| inst2: 동일 검증 및 기관 교대 | 34 | exit 0 |
| 실험 실패 후 정리 및 종료 코드 보존 | 35 | 예상 exit 23 |
| 시간 초과 후 cgroup·서버 정리 | 36 | 예상 exit 124 |
| `gpuq cancel` 후 정리 | 37 | 예상 exit 130 |
| da4 테스트 관리 기능 비활성화로 정리 실패 주입 | 38 | 예상 exit 75, hold 유지 |
| hold 중 다음 GPU 명령 미실행 → root 복구 후 실행 | 39 | exit 0, 복구 뒤에만 marker 출력 |
| 제어 서비스 cgroup SIGKILL → 재시작 시 hold → 남은 cgroup·서버 복구 | 40 | 예상 exit 75, 복구 성공 |
| 운영 예제 크기인 32G 풀 생성·읽기/쓰기·CUDA | 41 | exit 0 |

증거: [일반 시나리오](results/e2e-summary.json), [장애 시나리오](results/fault-e2e-summary.json), [32G 예제](results/32G-smoke-summary.json), 각 `e2e-*.log`.
기존 GPU 전용 명령도 job 11에서 `GPU_ONLY_REGRESSION_OK`로 확인했다.

## 기존 환경과 동시 사용 시 발견한 문제

기관2의 기존 GPU 벤치마크 job 32 종료(14:47:22) 뒤에 테스트 job 33을 실행했다.
그 뒤 기존 기관2 서버의 별도 재시작(14:47:26)이 겹쳤고, 기존 `nvme prepare`가
테스트 컨테이너의 `daos_server`를 발견하여 재시작을 거부했다. 물리 NVMe를 숨겨도
호스트는 컨테이너 프로세스를 볼 수 있으므로 관리 명령까지 분리되는 것은 아니다.

테스트 컨테이너 종료 후 기존 da3/da4 서비스를 **데이터 초기화 없이 다시 시작**했다.
기존 `da34_nvme` 두 rank joined 및 기존 풀 UUID 유지를 확인했다.
향후 파일 기반 테스트도 기존 시스템의 재시작/prepare와 시간을 조율해야 한다.
실제 운영 모드는 기존 호스트 서비스를 해제하고 한 기관의 lifecycle만 실행하도록 구성되어 있다.

기존 `da12_nvme` 두 rank는 사전 조회부터 excluded였다(상태 시각 11:31, 본 작업 시작 전).
이 상태는 이번 작업에서 변경하지 않았다. 기존 서비스 active와 DAOS rank joined는 별도로 확인해야 한다.

## 비파괴 검증 당시 상태와 당시 미실행 항목

- 테스트 컨테이너·파일 bdev·hugetlbfs mount·노드 lease 모두 없음: [nodes-final.json](results/nodes-final.json).
- 테스트용 제어 서비스 중지. 운영 broker/gpuqueue와 기존 agent 유지: [cxl2-final.txt](results/cxl2-final.txt).
- 당시 cxl2 및 da1~da4의 **물리 NVMe 모드 enabled=false**. 당시 `--inst` 제출은 비활성화 안내로 거부됐다.
- 기존 풀 UUID는 사전 목록의 부분집합으로 유지된다. 검증 중 별도 연구용 풀이 추가 생성됐다.
- 물리 NVMe `blkdiscard`, 기존 두 시스템 폐기·4대 통합 전환은 **실행하지 않았다**.
- NVMe 삭제 경로는 자동 테스트의 성공/실패·장치/lease 보호 검사를 수행했으며 하드웨어 삭제 검증은 남아 있다.
- 전환 도구의 기본 실행은 계획만 표시하며, `--execute`만으로는 삭제를 거부함을 확인했다.
  [계획](results/activation-plan.txt), [삭제 확인 플래그 검사](results/activation-guard.txt).
- 이 검증은 기능 검증이며, NVMe 성능 비교나 LMCache/GDS 전체 경로 성능 검증이 아니다.

이후 승인된 실제 전환과 하드웨어 검증 결과는 이 문서 상단에 기록했다.
