#!/bin/bash
set -euo pipefail
# Run inside an active gpuq --inst lease. No pre-created pools are assumed.
gpuq daos-exec -- env GPUQ_SMOKE_POOL_SIZE="${GPUQ_SMOKE_POOL_SIZE:-32G}" bash -euo pipefail -c '
  dmg -o /gpuq/daos_control.yml -j system query
  dmg -o /gpuq/daos_control.yml pool create --size="$GPUQ_SMOKE_POOL_SIZE" --nsvc=1 --nranks=4 smoke
  daos -G "$DAOS_SYSTEM" container create --type=POSIX smoke smoke
  printf "gpuq four-server DAOS roundtrip\n" > input.txt
  daos -G "$DAOS_SYSTEM" filesystem copy --src "$PWD/input.txt" --dst daos://smoke/smoke/input.txt
  daos -G "$DAOS_SYSTEM" filesystem copy --src daos://smoke/smoke/input.txt --dst "$PWD/output.txt"
  test "$(sha256sum < input.txt)" = "$(sha256sum < output.txt)"
  echo FOUR_SERVER_DAOS_ROUNDTRIP_OK
'
/mnt/nvme1/venvs/lmcache-prof-src/bin/python -c 'import torch; x=torch.ones(1024,device="cuda"); assert x.sum().item()==1024; print("GPU_CUDA_SMOKE_OK")'
