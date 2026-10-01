from xccode.versions import COMPONENTS, Pin, is_complete, load, validate

DIGEST = "0" * 64
DIGEST_FIELD = {"opencode": "sha256", "gitleaks": "sha256", "debian_image": "sha256"}
COMMIT_FIELD = {"openagentscontrol": "commit"}
DIGEST_ONLY = {"tei": "digest"}


def _complete_pins() -> dict[str, Pin]:
    pins: dict[str, Pin] = {}
    for name in COMPONENTS:
        checksum = ""
        if name in DIGEST_FIELD:
            checksum = DIGEST
        elif name in COMMIT_FIELD:
            checksum = "a" * 40
        elif name in DIGEST_ONLY:
            checksum = "sha256:" + DIGEST
        pins[name] = Pin(name, "1.0.0", checksum)
    return pins


def _write(tmp_path, pins: dict[str, Pin]) -> None:
    lines = []
    for name in COMPONENTS:
        p = pins[name]
        lines.append(f"[{name}]")
        lines.append(f'version = "{p.version}"')
        field = DIGEST_FIELD.get(name) or COMMIT_FIELD.get(name) or DIGEST_ONLY.get(name)
        if field:
            lines.append(f'{field} = "{p.checksum}"')
    (tmp_path / "versions.lock").write_text("\n".join(lines) + "\n")


def test_complete_lock_validates(tmp_path):
    pins = _complete_pins()
    _write(tmp_path, pins)
    loaded = load(tmp_path / "versions.lock")
    assert loaded == pins
    assert validate(loaded) == []
    assert is_complete(loaded)


def test_empty_template_fails_validation(tmp_path):
    empty = {name: Pin(name, "", "") for name in COMPONENTS}
    _write(tmp_path, empty)
    problems = validate(load(tmp_path / "versions.lock"))
    assert problems
    assert any("no version" in p for p in problems)


def test_missing_checksum_is_flagged(tmp_path):
    pins = _complete_pins()
    pins["opencode"] = Pin("opencode", "1.0.0", "")
    _write(tmp_path, pins)
    problems = validate(load(tmp_path / "versions.lock"))
    assert any("opencode: no sha256" in p for p in problems)


def test_missing_component_is_flagged():
    pins = _complete_pins()
    del pins["gitleaks"]
    assert any("missing pin for gitleaks" in p for p in validate(pins))


def test_unknown_component_is_flagged():
    pins = _complete_pins()
    pins["sneaky"] = Pin("sneaky", "1.0.0", "")
    assert any("unknown component sneaky" in p for p in validate(pins))


def test_version_only_components_need_no_checksum():
    pins = _complete_pins()
    pins["omniroute"] = Pin("omniroute", "1.0.0", "")
    assert validate(pins) == []
