from xccode.guardrails import (
    GuardConfig,
    apply,
    load_config,
    parse_passwd,
    plan,
    render_aios_dropin,
    render_audit_rules,
    render_nft,
)

# Documentation ranges only, so the public-repo "no internal name" check stays clean.
DOC_MESH = "192.0.2.0/24"

PASSWD = "\n".join(
    [
        "root:x:0:0:root:/root:/bin/bash",
        "marius:x:1000:1000::/home/marius:/bin/bash",
        "xccode:x:1001:1001::/nonexistent:/usr/sbin/nologin",
        "aios:x:997:987::/nonexistent:/usr/sbin/nologin",
        "xcloud:x:1002:1002::/nonexistent:/usr/sbin/nologin",
    ]
)


def test_parse_passwd_maps_names_to_uids():
    uids = parse_passwd(PASSWD)
    assert uids["aios"] == 997
    assert uids["xcloud"] == 1002
    assert uids["xccode"] == 1001
    assert "nobody" not in uids


def test_nft_starts_with_destroy_never_flush():
    accounts = parse_passwd(PASSWD)
    out = render_nft(GuardConfig(), accounts)
    assert out.splitlines()[1] == "destroy table inet xccode"
    assert "flush ruleset" not in out


def test_nft_aios_rule_only_when_aios_exists():
    no_aios = parse_passwd(PASSWD.replace("aios:x:997:987", "aios-disabled:x:997:987"))
    out = render_nft(GuardConfig(), no_aios)
    assert "skuid 997" not in out
    with_aios = render_nft(GuardConfig(), parse_passwd(PASSWD))
    assert "skuid 997" in with_aios


def test_nft_omits_internal_rules_when_config_is_empty():
    out = render_nft(GuardConfig(), parse_passwd(PASSWD))
    assert "ip daddr" not in out  # no mesh CIDR hardcoded
    assert "aios_ports" not in out


def test_nft_renders_mesh_and_aios_rejects_from_config():
    cfg = GuardConfig(mesh_cidrs=(DOC_MESH,), aios_ports=(11434, 8081, 9101))
    out = render_nft(cfg, parse_passwd(PASSWD))
    assert f"ip daddr {{ {DOC_MESH} }}" in out
    assert "tcp dport { 11434, 8081, 9101 }" in out
    assert "skuid 1001" in out  # xccode uid


def test_dropin_hides_xcloud_paths():
    out = render_aios_dropin(997)
    assert "InaccessiblePaths=/opt/xcloud /etc/xcloud /var/lib/xcloud" in out


def test_audit_rules_only_for_existing_accounts():
    out = render_audit_rules(parse_passwd(PASSWD))
    assert "-F uid=997 -k xccode-aios" in out
    assert "-F uid=1002 -k xccode-xcloud" in out
    only_aios = render_audit_rules(parse_passwd(PASSWD.replace("xcloud:x:1002", "xcloud-x:x:1002")))
    assert "xccode-xcloud" not in only_aios
    assert "xccode-aios" in only_aios


def test_plan_writes_dropin_only_when_aios_exists(tmp_path):
    writes = plan(GuardConfig(), parse_passwd(PASSWD), tmp_path)
    paths = {str(w.path) for w in writes}
    assert str(tmp_path / "nft" / "xccode.nft") in paths
    assert str(tmp_path / "systemd" / "user@997.service.d" / "40-xccode-paths.conf") in paths
    assert str(tmp_path / "audit" / "xccode.rules") in paths


def test_apply_creates_the_rendered_files(tmp_path):
    cfg = GuardConfig(mesh_cidrs=(DOC_MESH,))
    written = apply(cfg, parse_passwd(PASSWD), tmp_path)
    assert len(written) == 3
    nft = (tmp_path / "nft" / "xccode.nft").read_text()
    assert "destroy table inet xccode" in nft


def test_load_config_from_toml(tmp_path):
    p = tmp_path / "guardrails.toml"
    p.write_text(f'mesh_cidrs = ["{DOC_MESH}"]\naios_ports = [11434, 8081]\n')
    cfg = load_config(p)
    assert cfg.mesh_cidrs == (DOC_MESH,)
    assert cfg.aios_ports == (11434, 8081)
    assert cfg.xccode_ports == (18080, 18090, 18128, 18180, 18181)


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.toml")
    assert cfg.mesh_cidrs == ()
    assert cfg.aios_ports == ()
