from xccode.doctor import Check, Host, gather_passwd_group, groups_of, render, run_checks

PASSWD = "\n".join(
    [
        "root:x:0:0:root:/root:/bin/bash",
        "marius:x:1000:1000::/home/marius:/bin/bash",
        "xccode:x:1001:1001::/nonexistent:/usr/sbin/nologin",
        "aios:x:997:987::/nonexistent:/usr/sbin/nologin",
        "xcloud:x:1002:1002::/nonexistent:/usr/sbin/nologin",
    ]
)
GROUP = "\n".join(
    [
        "root:x:0:",
        "marius:x:1000:",
        "xccode:x:1001:",
        "xccode-users:x:2000:marius",
        "aios:x:987:",
        "xcloud:x:1002:",
    ]
)

ETC = "/etc/xcloud/xccode"
STATE = "/var/lib/xcloud/xccode"
OPT = "/opt/xcloud/xccode"


def host(**overrides) -> Host:
    passwd, gid_to_group, members = gather_passwd_group(PASSWD, GROUP)
    existing = frozenset(
        {
            ETC,
            STATE,
            OPT,
            f"{ETC}/nft/xccode.nft",
            "/etc/audit/rules.d/xccode.rules",
            "/etc/systemd/system/user@997.service.d/40-xccode-paths.conf",
        }
    )
    return Host(passwd=passwd, gid_to_group=gid_to_group, members=members, existing=existing)


def names(checks: list[Check]) -> dict[str, bool]:
    return {c.name: c.ok for c in checks}


def test_a_healthy_install_is_all_green():
    checks = run_checks(host())
    assert all(c.ok for c in checks), render(checks)


def test_xccode_with_a_login_shell_is_red():
    passwd, gid_to_group, members = gather_passwd_group(
        PASSWD.replace("xccode:x:1001:1001::/nonexistent:/usr/sbin/nologin",
                       "xccode:x:1001:1001::/nonexistent:/bin/bash"),
        GROUP,
    )
    h = host()
    checks = run_checks(Host(passwd, gid_to_group, members, h.existing))
    assert names(checks)["account-xccode"] is False


def test_marius_missing_from_xccode_users_is_red():
    group = GROUP.replace("xccode-users:x:2000:marius", "xccode-users:x:2000:")
    passwd, gid_to_group, members = gather_passwd_group(PASSWD, group)
    h = host()
    checks = run_checks(Host(passwd, gid_to_group, members, h.existing))
    assert names(checks)["group-xccode-users"] is False


def test_aios_in_xccode_users_is_red():
    passwd, gid_to_group, members = gather_passwd_group(
        PASSWD, GROUP.replace("xccode-users:x:2000:marius", "xccode-users:x:2000:marius,aios")
    )
    h = host()
    checks = run_checks(Host(passwd, gid_to_group, members, h.existing))
    assert names(checks)["no-aios-in-xccode"] is False


def test_missing_guard_files_are_red():
    h = host()
    sparse = h.existing - {f"{ETC}/nft/xccode.nft"}
    checks = run_checks(Host(h.passwd, h.gid_to_group, h.members, sparse))
    assert names(checks)["guard-nft"] is False


def test_dropin_not_required_when_aios_is_absent():
    passwd, gid_to_group, members = gather_passwd_group(
        PASSWD.replace("aios:x:997:987", "aios-absent:x:997:987"), GROUP
    )
    existing = frozenset(
        {ETC, STATE, OPT, f"{ETC}/nft/xccode.nft", "/etc/audit/rules.d/xccode.rules"}
    )
    checks = run_checks(Host(passwd, gid_to_group, members, existing))
    assert names(checks)["guard-dropin"] is True
    # audit rules still needed because xcloud exists
    assert names(checks)["guard-audit"] is True


def test_groups_of_includes_primary_group():
    passwd, gid_to_group, members = gather_passwd_group(PASSWD, GROUP)
    h = Host(passwd, gid_to_group, members, frozenset())
    assert "xccode" in groups_of(h, "xccode")  # primary group
    assert "xccode-users" in groups_of(h, "marius")  # supplementary


def test_render_summarises():
    text = render([Check("a", True, "ok"), Check("b", False, "bad")])
    assert "1/2 checks passed" in text
    assert "FAIL" in text and "OK" in text
