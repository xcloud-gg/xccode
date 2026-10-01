from xccode.hashing import doc_hash
from xccode.redact import (
    Pseudonymiser,
    sanitize,
    sanitize_advisor_input,
    store_sent,
)

# Documentation ranges only, so the public-repo "no internal name" check stays clean.
DOC_IP_A = "192.0.2.1"
DOC_IP_B = "198.51.100.7"
DOC_MAC = "00:11:22:33:44:55"
# Built at runtime so the secret shape never appears as a literal in the public repo.
CANARY_AWS = "AKIA" + "1234567890ABCDEF"


def test_canary_secret_is_redacted():
    out = sanitize(f"key={CANARY_AWS} host={DOC_IP_A}", Pseudonymiser())
    assert CANARY_AWS not in out
    assert "[REDACTED:" in out


def test_ips_and_macs_become_pseudonyms():
    out = sanitize(f"gw {DOC_IP_A} and {DOC_MAC}", Pseudonymiser())
    assert DOC_IP_A not in out and DOC_MAC not in out
    assert "ip-1" in out and "mac-1" in out


def test_pseudonyms_are_stable():
    p = Pseudonymiser()
    a = sanitize(f"{DOC_IP_A} {DOC_IP_A} {DOC_IP_B}", p)
    assert a.count("ip-1") == 2
    assert "ip-2" in a


def test_pseudonymiser_roundtrip_preserves_counters():
    p = Pseudonymiser()
    p.pseudonym("ip", DOC_IP_A)
    q = Pseudonymiser.from_dict(p.to_dict())
    assert q.pseudonym("ip", DOC_IP_A) == "ip-1"  # stable across reload
    assert q.pseudonym("ip", DOC_IP_B) == "ip-2"  # counter continuity


def test_sanitize_advisor_input_recurses():
    inputs = {
        "plan": {"text": f"reboot {DOC_IP_A}", "target": DOC_MAC},
        "secret": "password=hunter2hunter2secret",
    }
    out = sanitize_advisor_input(inputs, Pseudonymiser())
    assert DOC_IP_A not in str(out) and DOC_MAC not in str(out)
    assert "hunter2hunter2secret" not in str(out)
    assert out["plan"]["text"].count("ip-1") == 1


def test_store_sent_records_the_exact_payload(tmp_path):
    payload = {"text": f"touch {DOC_IP_A}"}
    h = store_sent(tmp_path, payload)
    assert h == doc_hash(payload)
    assert (tmp_path / f"{h}.sent.json").exists()


def test_ipv6_is_pseudonymised():
    out = sanitize("link 2001:db8::1 is up", Pseudonymiser())
    assert "2001:db8::1" not in out
    assert "ip-1" in out
