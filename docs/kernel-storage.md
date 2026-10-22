# 커널 스토리지 작업 (`--stor`)

`gpuq submit --stor lustre|nfsrdma|nvmeof [--nodes 1|4]` 작업은 da1~4 의 연구용 NVMe 위에
Lustre on ZFS, ZFS + 커널 NFSoRDMA, 커널 nvmet NVMe-oF 중 하나를 띄우고 cxl2 에 마운트한 뒤
실험을 실행한다. 끝나면 서버를 내리고 NVMe 를 DAOS 작업과 같은 방식으로 초기화한다.
`--inst` 작업과 같은 lease 를 쓰므로 둘은 동시에 돌지 않는다.

```bash
gpuq submit --stor lustre --nodes 4 --time 2h -- /home/me/run.sh   # payload 는 절대 경로
gpuq stor-ctl status        # 작업 안에서: 마운트·장치
gpuq stor-ctl cache-cold    # 서버 ARC(primarycache=metadata + 축출)·클라 페이지 캐시 비우기
gpuq stor-ctl cache-warm
```

| kind | `--nodes 1` | `--nodes 4` | cxl2 |
|---|---|---|---|
| lustre | da1: MGS+MDT0+OST0 한 풀 | da2~4 OST1~3 추가, `stripe_count -1` | `mount -t lustre` |
| nfsrdma | da1 커널 nfsd, RDMA 20049 | 미지원 | NFSv3 RDMA (da 의 `nfs.conf` 가 v4 를 끔) |
| nvmeof | da1 nvmet ns 1개 | 4대 | 1노드 XFS, 4노드 dm-striped(1 MiB) + XFS |

작업 환경변수: `GPUQ_STOR_KIND`, `GPUQ_STOR_NODES`, `GPUQ_STOR_PATH`(마운트, 제출자 소유),
`GPUQ_STOR_DEV`(NVMe-oF 블록 장치), `GPUQ_LEASE_ID`.

## 정리와 잔여물 검사

정리 순서: payload 정지 → cxl2 해체(umount, dm 제거, `nvme disconnect`) → 참여 노드 모두 정지 →
그 뒤에만 `zpool labelclear` → `blkdiscard` → `wipefs` 서명 없음 확인. Lustre 는 장치·마운트가
하나도 없을 때 `lustre_rmmod` 로 LNet 까지 내린다(ptlrpc 가 LNet 을 잡아 unconfigure 가 안 됨).

노드 헬퍼의 `residue()` 는 gpuq zpool, gpuq nvmet 서브시스템, gpuq NFS export, gpuq Lustre 마운트,
gpuq 가 켠 LNet, `zfs_arc_max` 기준값, NVMe 파티션·서명을 본다. **DAOS 작업의 preflight 도
이 검사를 통과해야 한다.** 정리에 실패하면 큐가 정지되고 root 가 `gpuqueue-daos recover` 로 푼다.

## 설정

broker(`config.json`)의 `stor`: `enabled`, `client_mount_root`, `server_ips`, `nvmeof_transport`
(`rdma`|`tcp`), `nvmeof_port`. 노드(`node.json`)의 `stor`: `enabled`, `index`, `fabric_ip`,
`fabric_iface`, `mgs_nid`, `client_ip`, `client_hostnqn`, `arc_max_job`, `arc_max_default`,
`nvmeof_transport`, `nvmeof_port`, `nvmet_port_id`. 배포는 기존 설정을 덮어쓰지 않으므로
`stor` 항목은 따로 병합한다.

## 장비 전제 (2026-10-22)

- da1~4: zfs 2.2.9·lustre 2.15.8 flexa(osd-zfs), inbox `nvmet-rdma`/`nvmet-tcp`·`rpcrdma`.
  `nfs-ganesha` 는 2049 를 점유하므로 비활성화되어 있어야 한다(preflight 가 거부).
- cxl2: Lustre 2.15.8 클라이언트, MOFED 24.10. 이 MOFED 는 NVMe-oF·NFSoRDMA 없이 설치되어
  `rpcrdma`·`nvme-rdma` 가 빈 모듈이다. `rpcrdma` 는 MOFED SRPM `mlnx-nfsrdma` 를 빌드해 해결했고,
  NVMe-oF 는 `nvmeof_transport: tcp`(IPoIB)로 운영한다. RDMA 는 MOFED 를 `--with-nvmf` 로
  재구성한 뒤에 켠다(커널 소스로 따로 빌드한 `nvme-rdma` 는 커널 크래시를 냈다).
- cxl2 의 `mdadm` 4.4 는 이 커널의 `md_mod` 와 맞지 않아 4노드 스트라이프는 device-mapper 를 쓴다.

## 검증 (2026-10-22, root, 큐 경유)

4 GiB O_DIRECT 쓰기 → cold/warm 읽기 → `cmp`: lustre1·lustre4·nfsrdma1·nvmeof1·nvmeof4(tcp) 통과.
사이사이 DAOS `--inst inst2` smoke 5회 모두 정상 기동·정리. 실패 경로(정리 실패 → 큐 정지 →
recover, 기동 실패 → 정리)도 실기에서 확인했다.
