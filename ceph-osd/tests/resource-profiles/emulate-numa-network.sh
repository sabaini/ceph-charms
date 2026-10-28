#!/bin/bash
# Copyright 2026 Canonical Ltd
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Turn an LXD VM into an emulated two-socket OSD host for the network
# binding functional test: a NUMA node per host socket, an NVMe disk on
# each node and a second NIC, with an address of its own, on node 1.
#
# Usage: emulate-numa-network.sh VM CPUS
#   VM    LXD instance of the (empty) Juju machine that will run ceph-osd
#   CPUS  limits.cpu taking CPUs from two host NUMA nodes, node 0's first,
#         e.g. 4-11,16-23 when the host has node0 0-11 and node1 12-23
#
# Needs a host with two NUMA nodes: LXD then gives the VM two nodes. The
# guest claims PCI locality by writing each device's numa_node, which the
# kernel accepts (and flags as a firmware bug) for emulation like this.
# Environment: BRIDGE (numa1br), SUBNET (10.77.1), POOL (default).
set -eu
VM=$1
CPUS=$2
BRIDGE=${BRIDGE:-numa1br}
SUBNET=${SUBNET:-10.77.1}
POOL=${POOL:-default}
MAC=00:16:3e:77:01:10

lxc network show "$BRIDGE" >/dev/null 2>&1 ||
    lxc network create "$BRIDGE" ipv4.address="$SUBNET.1/24" \
        ipv4.nat=true ipv6.address=none
# The machine is still empty; do not wait long for a guest shutdown.
lxc stop "$VM" --timeout 60 || lxc stop "$VM" --force
lxc config set "$VM" limits.cpu="$CPUS"
lxc config device add "$VM" numa1 nic network="$BRIDGE" hwaddr="$MAC"
for node in 0 1; do
    lxc storage volume create "$POOL" "$VM-osd-n$node" --type=block size=20GiB
    lxc config device add "$VM" "osd-n$node" disk pool="$POOL" \
        source="$VM-osd-n$node" io.bus=nvme
done
lxc start "$VM"
for _ in $(seq 60); do lxc exec "$VM" -- true 2>/dev/null && break; sleep 3; done

lxc exec "$VM" -- bash -s "$SUBNET" "$MAC" <<'GUEST'
set -eu
SUBNET=$1
MAC=$2
# The node 1 NIC routes its own source address through its own gateway.
cat > /etc/netplan/60-numa1.yaml <<EOF
network:
  version: 2
  ethernets:
    numa1:
      match: {macaddress: "$MAC"}
      set-name: numa1
      addresses: [$SUBNET.10/24]
      routes: [{to: 0.0.0.0/0, via: $SUBNET.1, table: 101}]
      routing-policy: [{from: $SUBNET.10, table: 101}]
EOF
chmod 600 /etc/netplan/60-numa1.yaml

cat > /usr/local/sbin/numa-fixup <<EOF
#!/bin/bash
# Report the node 1 NIC and NVMe disk on node 1, everything else on node 0.
pci() { readlink -f "\$1" | grep -o '[0-9a-f]\{4\}:[0-9a-f:.]\{7\}' | tail -1; }
for nic in /sys/class/net/*/device; do
    node=0; [ "\$(cat \${nic%/device}/address)" = $MAC ] && node=1
    echo \$node > /sys/bus/pci/devices/\$(pci \$nic)/numa_node
done
for ctrl in /sys/class/nvme/nvme*; do
    case "\$(cat \$ctrl/serial)" in *osd--n1*) node=1;; *) node=0;; esac
    echo \$node > /sys/bus/pci/devices/\$(pci \$ctrl/device)/numa_node
done
EOF
chmod +x /usr/local/sbin/numa-fixup
cat > /etc/systemd/system/numa-fixup.service <<EOF
[Unit]
Description=Emulate per-socket PCI NUMA locality
DefaultDependencies=no
After=systemd-udev-settle.service
Before=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/numa-fixup
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now numa-fixup.service
netplan apply 2>/dev/null
sleep 2
grep . /sys/devices/system/node/node*/cpulist
ip -br addr show numa1
ls /dev/disk/by-id/ | grep osd--n
GUEST
