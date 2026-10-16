# 기관 전달용: 신규 gpuq 사용 가이드 — DAOS 컨테이너 빌드·실행

> 이 문서는 고정 이미지 프로필을 사용하는 기존 구현의 상세 안내다.
> 사용자별 root 권한으로 슬롯 안에서 직접 DAOS를 운영하는 목표 방식은
> [설계 문서](docs/user-managed-daos.md)를 참고한다. 아래 RPM 빌드는 기존 배포 경로이며
> 패치·프로토타입용 이미지에 RPM 패키징이 필수라는 의미는 아니다.
>
> 공개본의 접속 주소와 장비 설정은 예제다. 실제 장비에 맞춰 설정하고,
> 기본 이미지 빌드의 `DAOS_RPM_REPO_URL`에는 접근 가능한 RPM 저장소 URL을 지정한다.

2026-10-16 기준. **실험은 본인 SSH 계정으로 `gpuq submit`에 제출합니다.** DAOS가 필요하면 `--inst inst1` 또는 `--inst inst2`를 지정합니다. 작업 하나가 A6000과 DAOS 서버 da1~da4 전체를 사용하고, 종료·실패·취소·시간 초과 시 컨테이너를 내린 뒤 연구용 NVMe와 작업 메타데이터를 초기화합니다. 이미지 빌드와 기관 프로필 등록은 6~8절을 참고하세요.

> 작성 시점에는 운영자 요청으로 **gpuq 유지보수 잠금** 상태입니다. 일반 사용자의 새 제출은 거부되며 운영자 개방 후 이용할 수 있습니다. 실제 상태는 `gpuq list`와 `gpuq daos-status`의 `maintenance`로 확인하세요.

**기존 `da12_nvme`·`da34_nvme` 시스템과 사전 제공 풀은 폐기했습니다. 기존 풀 UUID와 `/etc/daos/inst1.env`·`inst2.env`를 사용하는 안내는 더 이상 적용되지 않습니다.**

## 1. 이용 범위

각자 SSH 계정으로 cxl2에 접속합니다. `inst1`·`inst2`는 작업 환경 선택값이며 로그인 계정명이 아닙니다.

| 항목 | inst1 | inst2 |
|---|---|---|
| 주 연구 범위 | CXL 기반 LMCache | DAOS 기반 LMCache·GDS 연구 |
| GPU | A6000 48GB 1장 공동 사용 | 동일 |
| DAOS 서버 | 작업 시간 동안 da1·da2·da3·da4 전체 | 동일 |
| 새 시스템명 | `gpuq_inst1` | `gpuq_inst2` |
| 풀·DAOS 컨테이너 | 실험 스크립트에서 직접 생성 | 동일 |

DAOS는 서버당 NVMe 1개, engine/rank 1개, target 4개를 사용합니다. 전체 4 ranks/16 targets이며 HDD data tier는 사용하지 않습니다. 관리 서비스는 단일 replica인 연구용 구성입니다.

```bash
ssh -p2233 <본인계정>@ssh.example.org
id -nG
gpuq list
gpuq daos-status
gpuq gpu
```

GPU 큐 이용에는 `gpuqueue` 그룹이 필요합니다. 등록 후에는 SSH에 다시 접속합니다. 기존의 기관별 고정 풀 접근 그룹은 새 DAOS 환경을 선택하는 수단이 아닙니다. 코드 작성·빌드·CPU 점검은 일반 SSH 셸에서 수행합니다.

CXL 128GiB는 메모리 전용 NUMA node 1로 사용 가능합니다. `--inst inst1`이 CXL 메모리 배치나 LMCache offload를 자동 설정하지는 않습니다. 메모리 배치는 실험 코드에서 설정합니다. GPU+CXL만 사용하고 DAOS가 필요 없다면 아래의 GPU 전용 제출 방식을 사용합니다.

## 2. GPU 작업 제출과 로그

큐는 제출 순서대로 한 작업씩 실행합니다. 기본 제한은 4시간, 최대 8시간입니다. 처음에는 준비된 샘플로 확인하세요. 기관1은 `INST=inst1`, 기관2는 `INST=inst2`로 설정하고 운영자 개방 후 실행합니다.

```bash
INST=inst2
mkdir -p "$HOME/gpuq-smoke"
cd "$HOME/gpuq-smoke"

# DAOS와 GPU가 필요한 실험: 본인 기관의 --inst 선택
JOB_ID=$(gpuq submit --inst "$INST" --label first-smoke --time 5m -- bash /mnt/nvme1/gpuqueue/examples/daos-smoke.sh)
printf '작업 ID: %s\n' "$JOB_ID"

gpuq list
gpuq log "$JOB_ID"
gpuq follow "$JOB_ID"
gpuq wait "$JOB_ID"
# 취소가 필요할 때:
gpuq cancel "$JOB_ID"
```

**작업 ID는 제출 시 자동 생성되어 출력됩니다.** 임의로 만들지 않으며, 위 `JOB_ID`에는 실제 출력값이 저장됩니다. `--label`은 표시용 이름이고, DAOS 환경은 `--inst`로 선택합니다. `gpuq`만 실행하면 목록을 표시합니다.

제출 명령이 성공해 정수 ID가 출력된 뒤 로그를 조회합니다. 일반 사용자는 `sudo gpuq submit`을 사용하지 않습니다. 로그에서 `FOUR_SERVER_DAOS_ROUNDTRIP_OK`, `GPU_CUDA_SMOKE_OK`, `DAOS cleanup verified on all four servers.`와 정상 종료 코드 0을 확인합니다.

DAOS 없이 GPU만 쓸 때는 `--inst`를 생략합니다.

```bash
gpuq submit --label gpu-only --time 1h -- bash ./gpu_experiment.sh
```

- `queued`: 순서 대기, `running`: 슬롯 확보, `finished`: 종료. 실제 명령은 GPU 유휴·이전 DAOS 정리 완료를 확인한 뒤 시작합니다.
- `E-Level=0`: 정상 종료. 시간 초과는 124, DAOS 정리 실패는 75입니다. Task Spooler 목록은 취소 130을 `-126`으로 표시할 수 있습니다.
- 로그: `gpuq log JOB_ID` 또는 `/mnt/nvme1/gpuqueue/logs/<UID>/ts-out.*`. 조회·대기·취소는 제출자 또는 root가 수행합니다. `follow`의 Ctrl-C는 추적만 종료합니다.
- 제출 당시 디렉터리와 환경을 전달합니다. 대기 중 소스를 바꾸지 않거나 실험 버전을 고정하세요. SSH가 끊겨도 제출한 작업은 계속되지만 큐 재시작·호스트 재부팅 후 작업 자동 복원은 보장하지 않습니다.
- 재부팅 후 작업 ID가 재사용될 수 있으므로 중요한 작업은 로그 파일도 함께 보관합니다.
- 시간 제한은 슬롯 확보 후 GPU 대기·DAOS 준비·실험에 적용합니다. 이후 정리는 완료될 때까지 별도로 수행합니다.

## 3. 풀 생성과 읽기/쓰기 예제

아래를 본인 프로젝트의 `experiment.sh`로 저장한 뒤 `gpuq submit --inst ...`로 제출합니다. **독립 SSH 셸에서 `gpuq daos-exec`를 실행할 수는 없습니다.**

```bash
#!/bin/bash
set -euo pipefail

# 서버와 같은 버전의 클라이언트를 작업 소유 UID로 실행
gpuq daos-exec -- bash -euo pipefail -c '
  dmg -o "$DAOS_CONTROL_CONFIG" system query
  dmg -o "$DAOS_CONTROL_CONFIG" pool create --size=32G --nsvc=1 --nranks=4 my-pool
  daos -G "$DAOS_SYSTEM" container create --type=POSIX my-pool my-data
  printf "DAOS connection OK\n" > input.txt
  daos -G "$DAOS_SYSTEM" filesystem copy --src "$PWD/input.txt" --dst daos://my-pool/my-data/input.txt
  daos -G "$DAOS_SYSTEM" filesystem copy --src daos://my-pool/my-data/input.txt --dst "$PWD/output.txt"
  test "$(sha256sum < input.txt)" = "$(sha256sum < output.txt)"
  echo DAOS_READ_WRITE_OK
'

# 필요한 DAOS 결과를 호스트 파일로 복사한 뒤 종료
```

위 예제의 `32G`는 연결 확인용 풀 크기입니다. 실험에 맞게 생성하되 풀 서비스 replica 수와 데이터 복제 정책은 별도로 설계합니다. 고정 풀·컨테이너 이름이나 UUID는 제공하지 않습니다. 새 작업에는 `DAOS_SYSTEM`, `DAOS_AGENT_DRPC_DIR`, `DAOS_CONTROL_CONFIG`가 자동 설정됩니다.

위 스크립트를 본인 프로젝트의 `experiment.sh`로 저장한 뒤 제출합니다. 기관1은 `--inst inst1`을 사용합니다.

```bash
cd "$HOME/my-project"
JOB_ID=$(gpuq submit --inst inst2 --label my-experiment --time 4h -- bash ./experiment.sh)
printf '작업 ID: %s\n' "$JOB_ID"
```

**작업 종료 후 DAOS 데이터는 복구할 수 없습니다.** 유지할 결과는 작업이 끝나기 전에 cxl2의 본인 홈이나 결과 디렉터리로 복사하세요. 호스트의 작업 로그·결과 파일은 이 NVMe 초기화 대상이 아닙니다.

본인 Python 실험은 위 스크립트 안에서 본인 venv를 활성화하고 실행합니다. 다음 경로와 프로그램은 실제 프로젝트에 맞게 바꿉니다.

```bash
source "$HOME/venvs/my-lmcache/bin/activate"
python benchmark.py
```

준비된 연결 검증 스크립트도 사용할 수 있습니다. 풀 생성·읽기/쓰기와 A6000 CUDA 연산을 실행합니다.

```bash
mkdir -p "$HOME/gpuq-smoke"
cd "$HOME/gpuq-smoke"
gpuq submit --inst inst1 --time 5m --label smoke -- bash /mnt/nvme1/gpuqueue/examples/daos-smoke.sh
```

## 4. venv·LMCache와 버전 관리

- 기관별 venv·소스 checkout·LMCache 설정·포트·캐시 경로를 분리합니다. 공용 검증 venv `/mnt/nvme1/venvs/lmcache-prof-src`는 임의로 변경하지 않습니다.
- 상시 `gpuflow-lmcache` 서버는 중지·자동 시작 해제 상태입니다. LMCache·vLLM은 본인 큐 작업 안에서 시작하고 종료 시 함께 정리합니다. `nohup`, 분리된 tmux 또는 부모 종료 후 남는 백그라운드 서버를 사용하지 않습니다.
- `daos-exec`는 제출한 작업 디렉터리를 동일 경로에 마운트합니다. 호스트 venv가 컨테이너 안에서 그대로 동작한다고 가정하지 마세요. 컨테이너에서 필요한 Python 모듈은 이미지에 포함해야 합니다.
- 현재 서버·agent·클라이언트 이미지는 DAOS `2.8.1-3.exastor.202610140412.gd245523a.el8`에 고정되어 있습니다. 코드 변경 시 운영자가 유지보수 잠금·유휴 상태에서 모든 서버와 cxl2에 이미지를 배포하고 해당 기관 profile을 변경합니다. 실행 중 이미지 교체는 하지 않습니다.
- GDS 라이브러리, 컨테이너 내부 GPU 사용, LMCache KV offload와 성능 비교는 별도 구성·검증 대상입니다. 이번 연결·자동 정리 검증이 GDS 동작을 보장하지는 않습니다.

## 5. 유의사항과 장애 처리

- 두 기관이 동시에 DAOS 서버를 사용하지 않습니다. 작업 종료 시 네 서버의 정리가 모두 끝나야 다음 작업을 시작합니다.
- 정리 실패나 제어 서비스 강제 종료 시 recovery hold를 남깁니다. 목록이 `finished`여도 정리 중일 수 있으므로 `gpuq daos-status`를 확인하세요. hold 파일을 수동 삭제하거나 개인 큐로 우회하지 않습니다.
- 유지보수 잠금 중에는 `gpuq: maintenance locked`가 표시됩니다. 운영자의 해제 후 다시 제출합니다. 잠금 해제만으로 recovery hold가 해제되지는 않습니다.
- GPU 준비 대기 메시지가 나오면 `gpuq gpu`와 로그를 확인합니다. GPU compute 프로세스가 없고 메모리 사용량이 1,024MiB 이하일 때 시작합니다. 다른 기관 프로세스는 임의 종료하지 않습니다.
- GPU 큐는 FIFO이며 기관별 50:50 시간 보장은 없습니다. CPU·RAM·CXL·IB 부하와 공용 드라이버 변경도 조율합니다.
- insecure 관리 접속과 sudo 권한을 사용하는 협력적 연구환경입니다. `inst1`·`inst2` 선택은 기관 간 보안 경계가 아닙니다. 네 서버의 네이티브 DAOS 서비스를 임의로 다시 올리거나 저장장치 바인딩을 변경하지 않습니다.

문의 시 **계정명, inst1/inst2, 실제 작업 ID, 실행 명령, 오류 로그**를 전달하세요. 운영자는 `journalctl -u gpuqueue-daos`와 `/var/lib/gpuqueue-daos/jobs/<lease>/`의 서버별 정리 감사 기록을 확인하고 필요하면 `sudo /usr/local/libexec/gpuqueue-daos recover`로 복구합니다.

## 6. 기본 DAOS 컨테이너 이미지 빌드

**이미지는 필요할 때 한 번 빌드하고, gpuq는 등록된 이미지로 매 작업의 컨테이너를 시작합니다.** `gpuq submit --image` 옵션은 없습니다. `--inst`가 기관별 이미지 프로필을 선택합니다.

cxl2에서 아래 명령을 실행합니다. 이미지 관련 명령에는 `sudo podman`을 사용합니다. 일반 계정의 rootless Podman 이미지 저장소와 gpuq가 사용하는 root 저장소는 서로 다릅니다.

```bash
INST=inst2
IMAGE_DIR="$HOME/daos-images/$INST-rpm"
mkdir -p "$IMAGE_DIR"
cd "$IMAGE_DIR"
cp /usr/local/share/doc/gpuqueue-daos/Containerfile .
TAG="localhost/gpuq-daos-${INST}:2.8.1-20261016"
sudo podman build --build-arg DAOS_RPM_REPO_URL="$DAOS_RPM_REPO_URL" --build-arg DAOS_RPM_VERSION=2.8.1-3.exastor.202610140412.gd245523a.el8 -t "$TAG" .
```

제공된 Containerfile은 Rocky Linux 8에 사내 DAOS RPM 저장소를 설정하고 server·client·admin, RDMA 도구, Python을 설치합니다. 물리 NVMe 연결에 필요한 `kmod`(`modprobe`·`lsmod`)와 `pciutils`도 포함합니다.

현재 검증된 RPM은 `2.8.1-3.exastor.202610140412.gd245523a.el8`입니다. 다른 버전은 해당 RPM이 들어 있는 저장소와 의존성도 맞춰야 합니다. `latest`에서 이전 RPM이 사라진 경우 해당 빌드의 고정 저장소나 보관한 RPM을 사용합니다.

빌드 후 도구와 패키지를 확인합니다. 이 명령들은 DAOS 서버를 기동하거나 NVMe를 포맷하지 않습니다.

```bash
sudo podman run --rm --pull=never "$TAG" version
sudo podman run --rm --pull=never --entrypoint /usr/bin/daos_agent "$TAG" version
sudo podman run --rm --pull=never --entrypoint /usr/bin/dmg "$TAG" version
sudo podman run --rm --pull=never --entrypoint /usr/bin/daos "$TAG" version
sudo podman run --rm --pull=never --entrypoint /bin/bash "$TAG" -euc 'rpm -q daos-server daos-client daos-admin; command -v modprobe; command -v lsmod; command -v lspci'
```

태그를 만드는 것만으로 gpuq의 실행 버전이 바뀌지는 않습니다. 8절의 배포·등록이 필요합니다.

## 7. DAOS 소스 수정 후 컨테이너 빌드

흐름은 **소스 커밋 고정 → 빌드 컨테이너에서 RPM 생성 → RPM을 실행 이미지에 설치 → 5대 배포·기관 프로필 등록**입니다. 서버·agent·클라이언트와 해당 빌드의 라이브러리를 한 이미지에 담습니다.

### 7-1. 소스에서 RPM 생성

사내 `exastor/daos`의 `ci/gitlab/Dockerfile.daos-build`와 `ci/gitlab/build-rpms.sh`를 사용합니다. 빌드 스크립트가 Podman 컨테이너 안에서 `scons`와 RPM 패키징을 실행합니다.

다음은 해당 파일이 있는 **독립 git clone**을 cxl2에 준비한 경우입니다. `DAOS_SOURCE_DIR`는 본인 소스 경로로 바꿉니다. GitLab 접근 권한이 없으면 운영자가 동일 구조의 소스 checkout을 제공할 수 있습니다. 소스를 수정한 뒤 커밋하고 필요한 submodule을 준비합니다.

```bash
INST=inst2
DAOS_SOURCE_DIR="$HOME/src/daos"
cd "$DAOS_SOURCE_DIR"
DAOS_SOURCE_DIR=$(pwd -P)
git submodule update --init --recursive
git status --short
```

`git status --short`가 비어 있고 `.git`이 디렉터리인 독립 clone에서 진행합니다. 일반 사용자의 원본 소스는 유지하고, 새 빌드용 사본을 root 소유로 만들어 컴파일합니다.

```bash
SOURCE_SHA=$(git rev-parse HEAD)
SOURCE_SHORT_SHA=$(git rev-parse --short=12 HEAD)
SOURCE_REF=$(git rev-parse --abbrev-ref HEAD)
BUILD_DIR="$HOME/daos-build/${INST}-${SOURCE_SHORT_SHA}-$(date +%Y%m%d%H%M%S)"
(
set -euo pipefail
test -d .git
test -z "$(git status --porcelain)"
test -f ci/gitlab/build-rpms.sh
test -f ci/gitlab/Dockerfile.daos-build
sudo mkdir -p "$BUILD_DIR"
sudo cp -a "$DAOS_SOURCE_DIR" "$BUILD_DIR/source"
sudo chown -R root:root "$BUILD_DIR/source"
sudo env CI_COMMIT_SHA="$SOURCE_SHA" CI_COMMIT_SHORT_SHA="$SOURCE_SHORT_SHA" CI_COMMIT_REF_NAME="$SOURCE_REF" \
  bash -euc 'cd "$1"; exec bash ci/gitlab/build-rpms.sh' bash "$BUILD_DIR/source"
)
```

RPM은 `$BUILD_DIR/source/out/rpms/`에 생성됩니다. 이 디렉터리에는 DAOS와 의존 라이브러리 RPM, `repodata/`, `BUILD_INFO`가 있어야 합니다. 스크립트는 설치 스모크까지 수행하며, 위 로컬 호출 자체는 RPM을 공유 저장소에 게시하지 않습니다.

빌드는 CPU·RAM·디스크를 사용하고 공통 prerequisite 캐시 볼륨을 공유하므로 동시 실행하지 않습니다. 실험 사용자와 빌드 시간을 조율합니다. 기존 GitLab CI의 `rpm` 잡 산출물이 있으면 이 단계 대신 그 잡의 `out/rpms/`를 사용해도 됩니다.

### 7-2. RPM 묶음으로 실행 이미지 생성

7-1과 같은 셸에서 이어서 실행합니다. CI 산출물을 사용하면 `RPM_DIR`는 다운로드해 풀어 둔 `out/rpms` 경로로 바꾸고 `SOURCE_SHORT_SHA`도 해당 빌드의 소스 커밋으로 설정합니다.

```bash
RPM_DIR="$BUILD_DIR/source/out/rpms"
IMAGE_DIR="$HOME/daos-images/${INST}-${SOURCE_SHORT_SHA}-$(date +%Y%m%d%H%M%S)"
TAG="localhost/gpuq-daos-${INST}:${SOURCE_SHORT_SHA}"
(
set -euo pipefail
mkdir -p "$IMAGE_DIR"
sudo cp -a "$RPM_DIR" "$IMAGE_DIR/rpms"
sudo chown -R "$(id -u):$(id -g)" "$IMAGE_DIR/rpms"
cp /usr/local/share/doc/gpuqueue-daos/Containerfile.rpms "$IMAGE_DIR/Containerfile"
test -d "$IMAGE_DIR/rpms/repodata"
test -f "$IMAGE_DIR/rpms/BUILD_INFO"
cd "$IMAGE_DIR"
sudo podman build -t "$TAG" .
)
```

`Containerfile.rpms`는 `rpms/`를 이미지 내부의 로컬 RPM 저장소로 사용해 DAOS server·client·admin과 의존성을 설치합니다. 소스 빌드 정보는 `/usr/share/gpuq-daos/BUILD_INFO`에 남깁니다. RPM은 Rocky Linux 8/x86_64용이어야 합니다.

6절의 도구·패키지 확인 명령도 실행하고, 자체 빌드 정보는 아래로 확인합니다.

```bash
sudo podman run --rm --pull=never --entrypoint /bin/cat "$TAG" /usr/share/gpuq-daos/BUILD_INFO
```

여기서 설명한 소스 빌드는 해당 사내 저장소의 빌드 흐름입니다. CUDA/cuFile·GDS 옵션과 추가 라이브러리가 필요한 브랜치는 해당 요구사항을 함께 적용해야 합니다. **이미지 빌드 성공만으로 GDS나 LMCache KV offload가 검증되지는 않습니다.**

## 8. 이미지 배포와 기관별 버전 등록

### 8-1. 이미지 보관 및 운영자 전달

6절 또는 7절에서 사용한 같은 `INST`, `TAG`, `IMAGE_DIR`가 설정된 셸에서 실행합니다. 태그, 전체 이미지 ID, RPM 버전 또는 소스 커밋, `BUILD_INFO`, 아카이브 경로를 함께 기록합니다.

```bash
cd "$IMAGE_DIR"
IMAGE_ID=$(sudo podman image inspect "$TAG" --format '{{.Id}}')
IMAGE_ID="sha256:${IMAGE_ID#sha256:}"
printf 'INST=%s\nTAG=%s\nIMAGE_ID=%s\n' "$INST" "$TAG" "$IMAGE_ID" | tee image-info.txt
sudo podman save --format docker-archive -o "$PWD/gpuq-daos-image.tar" "$TAG"
sudo sha256sum gpuq-daos-image.tar > gpuq-daos-image.tar.sha256
```

gpuq는 태그가 아닌 **전체 `sha256:...` 이미지 ID**를 사용합니다. 아카이브 파일의 `sha256sum`은 전송 검사용으로 이미지 ID와 다른 값입니다. 이전 이미지·RPM·소스 정보는 재현에 필요하면 보관합니다.

이하 배포·등록은 cxl2의 root 권한과 da1~da4 SSH 키 인증을 사용할 수 있는 운영자가 수행합니다. 기관별로 다른 이미지 버전을 등록해 순차 사용할 수 있습니다.

### 8-2. 유지보수 잠금과 유휴 확인

```bash
sudo gpuq lock --reason 'DAOS image update'
gpuq list
gpuq daos-status
gpuq gpu
```

잠금은 실행 중인 작업을 강제로 종료하지 않습니다. 실행·대기 작업이 없고 GPU가 비어 있으며 `active`와 `blocked`가 `null`인 상태에서 진행합니다. 기존 설정을 백업합니다. 이미 있던 유지보수 잠금은 운영 일정에 맞게 유지합니다.

### 8-3. 같은 아카이브를 da1~da4에 로드

8-1의 아카이브 디렉터리에서 실행하고 `IMAGE_ID`는 `image-info.txt`의 전체 ID로 설정합니다. cxl2에도 같은 ID의 root Podman 이미지가 있어야 합니다.

```bash
(
set -euo pipefail
sudo podman image exists "$IMAGE_ID"
for host in da1 da2 da3 da4; do
  sudo scp gpuq-daos-image.tar gpuq-daos-image.tar.sha256 "$host:/var/tmp/"
  sudo ssh "$host" 'set -e
    cd /var/tmp
    sha256sum -c gpuq-daos-image.tar.sha256
    podman load -i gpuq-daos-image.tar'
  sudo ssh "$host" podman image exists "$IMAGE_ID"
done
)
```

각 호스트의 확인 명령이 성공한 뒤 등록합니다. 로드·검사 실패 시 설정 변경을 중지하고 잠금을 유지합니다.

### 8-4. 선택한 기관 프로필의 이미지 ID 변경

아래에서 **선택한 기관 항목**을 같은 전체 이미지 ID로 수정합니다. 저장장치 allowlist·시리얼·PCI·시스템명·포트 등 다른 설정은 기존 값을 유지합니다.

| 호스트 | 설정 파일 | 수정 필드 (`inst2` 예) |
|---|---|---|
| cxl2 | `/etc/gpuqueue-daos/config.json` | `profiles.inst2.client_image` |
| da1~da4 각각 | `/etc/gpuqueue-daos/node.json` | `profiles.inst2.image`, 존재하는 `client_image` |

cxl2 프로필에 별도 `server_image`가 있으면 그 값도 서버 이미지 ID와 맞춥니다. 이 가이드는 server·agent·client를 함께 담은 단일 이미지를 5대에 사용하는 방식입니다.

태그만 덮어쓰거나 cxl2 한 대의 설정만 바꾸면 적용되지 않습니다. 노드의 이미지·시스템명·포트가 프로필과 다르면 gpuq가 시작 전에 거부합니다.

수정한 각 JSON은 `python3 -m json.tool 파일경로`로 구문 확인하고 root 소유·0600 권한으로 유지합니다. 5대 설정을 맞춘 뒤 cxl2 broker만 다시 읽힙니다.

```bash
sudo systemctl restart gpuqueue-daos
gpuq daos-status
```

이미지 교체에 `gpuqueue.service` 재시작이나 최초 전환 도구 `gpuqueue-daos-activate` 실행은 필요하지 않습니다. cxl2 IB 인터페이스는 현재 `ibp65s0`이며 `client_fabric_iface`로 설정합니다. 슬롯 변경 후에는 실제 인터페이스와 IP를 확인합니다.

### 8-5. 새 이미지 검증 후 개방

잠금 중에는 root 검증 작업만 실행할 수 있습니다. 다음은 운영자 검증용입니다.

```bash
INST=inst2
sudo mkdir -p "/mnt/nvme1/gpuqueue/image-check/$INST"
cd "/mnt/nvme1/gpuqueue/image-check/$INST"
CHECK_JOB=$(sudo gpuq submit --inst "$INST" --time 5m --label image-check -- bash /mnt/nvme1/gpuqueue/examples/daos-smoke.sh)
sudo gpuq wait "$CHECK_JOB"
sudo gpuq log "$CHECK_JOB"
gpuq daos-status
```

네 ranks joined·풀 생성·읽기/쓰기·CUDA·자동 정리 성공과 `active=null`, `blocked=null`을 확인합니다. 검증 데이터도 종료 후 삭제됩니다. 운영자가 개방하는 시점에 `sudo gpuq unlock`을 실행합니다.

## 9. 이미지 빌드·실행 문제 해결

| 증상 | 확인 및 조치 |
|---|---|
| 빌드한 이미지가 gpuq에서 보이지 않음 | `sudo podman` 저장소, 5대 로드, 전체 이미지 ID 등록 확인 |
| 이미지·시스템명·포트 불일치 | 운영자가 기관 프로필과 네 노드 설정 대조 |
| `No active DAOS lease` | `daos-exec`를 `--inst` 작업 스크립트 안에서 실행 |
| 컨테이너에서 Python import 실패 | 해당 모듈을 이미지에 포함하거나 호환되는 호스트 venv 경로 사용 |
| `modprobe: command not found` | 실행 이미지에 `kmod` 설치 여부 확인 |
| exit 75 또는 `blocked` 존재 | 운영자에게 작업 ID·로그·이미지 ID 전달. hold 파일을 지워 우회하지 않음 |

문의 시 **기관명, 계정명, 실제 작업 ID, 이미지 ID, 소스 커밋/RPM 버전, 실행 명령, 오류 로그**를 함께 전달하세요.
