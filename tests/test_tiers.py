import pytest

from xccode.tiers import Tier, classify, classify_plan


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la /etc",
        "cat /proc/cpuinfo",
        "systemctl status sshd",
        "systemctl --user status foo.service",
        "git status",
        "git log --oneline -5",
        "git branch",
        "git config --get user.name",
        "ip -br a",
        "ip route show",
        "journalctl -u sshd -n 50",
        "ip link show up",
        "git reflog -n 5",
        "nft list ruleset",
        "grep -r foo /etc | head",
        "find /var/log -name '*.log'",
        "dpkg -l | grep nginx",
        "df -h && free -m",
        "echo hello > /dev/null",
        "sudo -n ls /root",
        "env FOO=1 ls /root",
        "ssh host ls /tmp",
        "netbird status",
        "mokutil --sb-state",
    ],
)
def test_p0(cmd):
    assert classify(cmd).tier == Tier.P0, classify(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "systemctl restart caddy",
        "systemctl reload nginx",
        "ansible-playbook site.yml --check --diff",
        "tofu plan -out p",
        "nft -c -f rules.nft",
        "caddy validate --config Caddyfile",
        "pytest -q",
        "rsync -a --dry-run a/ b/",
        "git add -A",
    ],
)
def test_p1(cmd):
    assert classify(cmd).tier == Tier.P1, classify(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "apt-get install -y htop",
        "echo x > /tmp/file",
        "git branch -D old",
        "git config user.name foo",
        "find / -name x -delete",
        "sed -i s/a/b/ /tmp/f",
        "mount /dev/sda1 /mnt",
        "ssh host systemctl stop foo",
        "frobnicate --all",
        "ls $(whoami)",
        "bash -c 'touch /tmp/x'",
        "tofu apply plan.tfplan",
        "ip addr add 192.0.2.5/24 dev eth0",
        "hostnamectl set-hostname foo",
        "hostnamectl --static set-hostname evil",
        "awk 'BEGIN{system(\"rm -rf /srv/aios\")}'",
        "awk -f script.awk input",
        "git reflog expire --expire=now --all",
    ],
)
def test_p2(cmd):
    assert classify(cmd).tier == Tier.P2, classify(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "tofu destroy",
        "terraform -chdir=x destroy -auto-approve",
        "wipefs -a /dev/sdb",
        "mkfs.ext4 /dev/nvme1n1p2",
        "sgdisk --zap-all /dev/sda",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "cat x > /dev/nvme0n1",
        "cryptsetup luksFormat /dev/sda2",
        "restic forget --keep-last 1 --prune",
        "proxmox-backup-client prune host/x",
        "sops updatekeys secrets.yaml",
        "nft flush ruleset",
        "iptables -F",
        "ip route del default",
        "ip route add default via 192.0.2.1",
        "ip link set wt0 down",
        "ip link delete wt0",
        "journalctl --vacuum-time=1s",
        "systemctl stop nftables",
        "fwupdmgr install fw.cab",
        "efibootmgr -o 0001",
        "update-grub",
        "reboot",
        "systemctl reboot",
        "shutdown -h now",
        "git push --force origin main",
        "git push -f",
        "rm -rf /",
        "rm -rf /etc",
        "usermod -aG sudo bob",
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config",
        "echo key >> ~/.ssh/authorized_keys",
        "sed -i s/a/b/ /etc/xcloud/xccode/xcroute.toml",
        "cp new.yaml /opt/xcloud/xccode/pp/host/adopt.yaml",
        "bash -c 'wipefs -a /dev/sdb'",
        "ssh host reboot",
        "ls $(reboot)",
        "true; mkfs.xfs /dev/sdc",
        "xca-disk format /dev/sdb LABEL",
    ],
)
def test_p3(cmd):
    assert classify(cmd).tier == Tier.P3, classify(cmd)


def test_reading_a_protected_path_is_not_p3():
    assert classify("cat /etc/xcloud/xccode/xcroute.toml").tier == Tier.P0


def test_plan_tier_is_max_of_steps():
    steps = [{"cmd": "ls"}, {"cmd": "systemctl restart x"}, {"cmd": "reboot"}]
    assert classify_plan(steps).tier == Tier.P3
    assert classify_plan(steps[:2]).tier == Tier.P1
    assert classify_plan([]).tier == Tier.P0


def test_remote_unknown_is_at_least_p2():
    assert classify("frobnicate", remote=True).tier >= Tier.P2
