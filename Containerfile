# Build once, then pin the resulting image ID in all institution profiles.
FROM quay.io/rockylinux/rockylinux:8
ARG DAOS_RPM_VERSION=2.8.1-3.exastor.202610140412.gd245523a.el8
ARG DAOS_RPM_REPO_URL
RUN test -n "$DAOS_RPM_REPO_URL"
RUN dnf -y install epel-release
RUN printf '[daos-research]\nname=DAOS research\nbaseurl=%s\nenabled=1\ngpgcheck=0\n' "$DAOS_RPM_REPO_URL" > /etc/yum.repos.d/daos-research.repo \
    && dnf -y --enablerepo=powertools --setopt=install_weak_deps=False install daos-server-${DAOS_RPM_VERSION} daos-client-${DAOS_RPM_VERSION} daos-admin-${DAOS_RPM_VERSION} libibverbs-utils procps-ng python3 \
    && dnf clean all
# Physical NVMe startup resets/rebinds PCI through SPDK's setup.sh.
# DAOS RPM dependencies alone do not install these utilities.
RUN dnf -y --setopt=install_weak_deps=False install kmod pciutils \
    && command -v modprobe && command -v lsmod && command -v lspci \
    && dnf clean all
ENTRYPOINT ["/usr/bin/daos_server"]
CMD ["version"]
