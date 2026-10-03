import pytest

from xccode.xcroute.auth import TokenAuth, digest
from xccode.xcroute.budget import BudgetGate, Limits
from xccode.xcroute.decide import (
    Answers,
    PoolScore,
    TurnTracker,
    mode_from,
    pick_pool,
    rules_answers,
)
from xccode.xcroute.events import EventLog
from xccode.xcroute.guard import redact, redact_messages
from xccode.xcroute.markers import RepoPolicy, repo_policy
from xccode.xcroute.router import (
    AUTO,
    Completion,
    PoolConfig,
    ProviderError,
    Request,
    Router,
)

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
ALL = {"coding-strong", "coding-fast", "reasoning", "fast"}
SCORES = {
    "coding-strong": PoolScore(0.9, 0.8, 0.6),
    "coding-fast": PoolScore(0.7, 0.2, 0.2),
    "reasoning": PoolScore(0.85, 0.9, 0.9),
    "fast": PoolScore(0.5, 0.05, 0.05),
}


# ---- Guard-lite ------------------------------------------------------------------------------
def test_guard_redacts_and_names_rules_without_leaking_the_match():
    r = redact(f"key {AWS} and token {GH}")
    assert AWS not in r.text and GH not in r.text
    assert r.hits == ("aws-access-key-id", "github-token")


def test_guard_private_key_block_even_unterminated():
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    r = redact(f"x\n{begin}\nMIIEabc\ndef")
    assert "MIIE" not in r.text and r.hits == ("private-key",)


def test_guard_clean_text_untouched():
    r = redact("please rename the function foo to bar")
    assert r.text == "please rename the function foo to bar" and r.hits == ()


def test_guard_redacts_age_and_vault_secrets():
    age = "AGE-SECRET-KEY-1" + "A" * 50
    hvs = "hvs." + "a" * 20
    r = redact(f"key {age} and token {hvs}")
    assert age not in r.text and hvs not in r.text
    assert set(r.hits) == {"age-secret-key", "vault-token"}


def test_guard_messages_all_shapes_and_originals_unchanged():
    msgs = [{"role": "user", "content": [{"type": "text", "text": AWS}]},
            {"role": "tool", "content": "password = hunter2hunter2hunter2"}]
    out, hits = redact_messages(msgs)
    assert AWS not in str(out) and "hunter2" not in str(out)
    assert set(hits) == {"aws-access-key-id", "assigned-secret"}
    assert AWS in str(msgs)  # input not mutated


# ---- markers ---------------------------------------------------------------------------------
def test_markers(tmp_path):
    repo = tmp_path / "r"
    (repo / "sub").mkdir(parents=True)
    assert repo_policy(repo / "sub") is RepoPolicy.NORMAL
    (repo / ".xccode-internal").touch()
    assert repo_policy(repo / "sub") is RepoPolicy.INTERNAL
    (repo / ".xccode-secret").touch()
    assert repo_policy(repo / "sub") is RepoPolicy.SECRET


def test_markers_unknown_cwd_is_not_normal_and_override_by_remote(tmp_path):
    assert repo_policy(None) is RepoPolicy.INTERNAL
    assert repo_policy("relative/dir") is RepoPolicy.INTERNAL
    ov = {"git@h:o/r.git": RepoPolicy.SECRET}
    assert repo_policy(tmp_path, "git@h:o/r.git", ov) is RepoPolicy.SECRET
    assert repo_policy(tmp_path, "git@h:o/other.git", ov) is RepoPolicy.NORMAL


# ---- auth ------------------------------------------------------------------------------------
def test_auth():
    a = TokenAuth({"opencode": digest("t-oc"), "hermes": digest("t-hm")})
    assert a.agent_for("t-oc") == "opencode" and a.agent_for("t-hm") == "hermes"
    assert a.agent_for("nope") is None and a.agent_for(None) is None and a.agent_for("") is None
    with pytest.raises(ValueError):
        TokenAuth({"mallory": digest("x")})


# ---- budget ----------------------------------------------------------------------------------
def test_budget_caps_and_day_roll():
    g = BudgetGate(Limits(per_request=100, per_day=150, per_provider_day={"p": 120},
                          per_agent_day={"hermes": 50}))
    assert not g.allows("d1", "opencode", "p", 101)  # per request
    assert g.allows("d1", "opencode", "p", 100)
    g.record("d1", "opencode", "p", 100)
    assert not g.allows("d1", "opencode", "p", 30)  # provider 130 > 120
    assert not g.allows("d1", "opencode", "q", 60)  # day 160 > 150
    assert g.allows("d1", "opencode", "q", 50)
    assert not g.allows("d1", "hermes", "q", 51)  # agent cap
    assert g.allows("d2", "opencode", "p", 100)  # new day, fresh counters


def test_budget_persists_across_restart(tmp_path):
    state = tmp_path / "budget.json"
    g = BudgetGate(Limits(per_request=100, per_day=200), state_path=state)
    g.record("d1", "opencode", "p", 100)
    # a fresh gate (a simulated restart) resumes from the persisted total
    g2 = BudgetGate(Limits(per_request=100, per_day=200), state_path=state)
    assert not g2.allows("d1", "opencode", "p", 150)  # 100 + 150 > 200
    assert g2.allows("d1", "opencode", "p", 100)  # 100 + 100 == 200
    assert g2.allows("d2", "opencode", "p", 100)  # a new day starts fresh


# ---- decide ----------------------------------------------------------------------------------
def test_rules_baseline_modes():
    assert mode_from(rules_answers("fix the failing test in parser.py")) == "coding"
    assert mode_from(rules_answers("what does this function do?")) == "fast"
    assert mode_from(rules_answers("investigate the root cause and compare designs")) == "reasoning"


def test_floor_code_change_never_below_coding_strong():
    code = Answers(0.1, 0.9, 0.1)
    pool, why = pick_pool("coding", code, "unknown", {"coding-fast", "fast"}, SCORES, 0.2, 0.1)
    assert pool is None and why == "no-healthy-pool"  # refuse, do not downgrade
    pool, _ = pick_pool("coding", code, "unknown", ALL, SCORES, 0.2, 0.1)
    assert pool == "coding-strong"


def test_floor_by_role_even_when_answers_say_explain():
    explain = Answers(0.1, 0.1, 0.9)
    pool, _ = pick_pool("fast", explain, "coder", ALL, SCORES, 0.2, 0.1)
    assert pool in {"coding-strong", "reasoning"}
    pool, _ = pick_pool("fast", explain, "contextscout", ALL, SCORES, 0.2, 0.1)
    assert pool == "fast"


def test_fast_needs_confidence():
    def pool(p):
        return pick_pool("fast", Answers(0.1, 0.1, p), "unknown", ALL, SCORES, 0.2, 0.1)[0]

    assert pool(0.7) == "reasoning" and pool(0.8) == "fast"


def test_score_trades_quality_against_cost():
    two = {"coding-strong": PoolScore(0.90, 0.9, 0.5), "coding-fast": PoolScore(0.88, 0.1, 0.1)}
    # floor excludes coding-fast for code; for a non-code coding-mode turn the score decides
    non_code = Answers(0.1, 0.4, 0.1)
    assert pick_pool("coding", non_code, "unknown", ALL, two, 0.2, 0.1)[0] == "coding-fast"
    assert pick_pool("coding", non_code, "unknown", ALL, two, 0.0, 0.0)[0] == "coding-strong"


def test_unsure_and_turn_tracker():
    assert Answers(0.55, 0.45, 0.5).unsure() and not Answers(0.9, 0.5, 0.5).unsure()
    t = TurnTracker()
    assert t.sticky("s", "hello") is None
    t.remember("s", "hello", "fast")
    assert t.sticky("s", "hello") == "fast" and t.sticky("s", "other") is None


# ---- router ----------------------------------------------------------------------------------
class Spy:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, pool, messages):
        self.calls.append((pool, messages))
        if self.fail:
            raise ProviderError("boom")
        return Completion("ok", 10, 5, 1000)


def make(tmp_path, provider=None, healthy=ALL, per_day=10_000_000, roles=None):
    return Router(
        auth=TokenAuth({"opencode": digest("tok"), "hermes": digest("hm")}),
        pools={p: PoolConfig("prov-" + p, 1000) for p in ALL},
        scores=SCORES, healthy=lambda: set(healthy),
        budget=BudgetGate(Limits(per_request=100_000, per_day=per_day,
                                 per_agent_day={"hermes": 1500})),
        provider=provider or Spy(), events=EventLog(tmp_path / "x.db"),
        role_by_prompt_hash=roles or {},
    ), tmp_path


def req(text="fix the bug in a.py", token="tok", model=AUTO, cwd="/work/r", session="s1", extra=()):
    return Request(token, model, [*extra, {"role": "user", "content": text}], session, cwd)


def test_router_bad_token_401_and_no_provider_call(tmp_path):
    spy = Spy()
    r, _ = make(tmp_path, provider=spy)
    assert r.handle(req(token="x")).status == 401 and spy.calls == []


def test_router_secret_repo_refused_before_provider(tmp_path):
    (tmp_path / ".xccode-secret").touch()
    spy = Spy()
    r, _ = make(tmp_path, provider=spy)
    res = r.handle(req(cwd=str(tmp_path)))
    assert res.status == 403 and spy.calls == []
    assert r.events.all()[0]["outcome"] == "refused:secret-repo"


def test_router_redacts_before_provider_and_event_has_no_prompt_text(tmp_path):
    spy = Spy()
    r, _ = make(tmp_path, provider=spy)
    res = r.handle(req(text=f"fix this, key {AWS}"))
    assert res.status == 200 and AWS not in str(spy.calls)
    ev = r.events.all()[0]
    assert ev["guard_hits"] == ["aws-access-key-id"] and "fix this" not in str(ev)
    assert ev["pool"] == "coding-strong" and ev["decider"] == "rules"


def test_router_sticky_through_tool_loop_and_pinned_passthrough(tmp_path):
    r, _ = make(tmp_path)
    a = r.handle(req(text="fix it"))
    b = r.handle(req(text="fix it"))  # same user message: tool loop continues
    assert (a.event.decider, b.event.decider) == ("rules", "sticky")
    p = r.handle(req(text="explain", model="fast"))
    assert p.event.decider == "pinned" and p.pool == "fast"  # floor not applied to pinned
    assert r.handle(req(model="gpt-x")).status == 400


def test_router_no_healthy_floor_pool_is_503_not_a_downgrade(tmp_path):
    spy = Spy()
    r, _ = make(tmp_path, provider=spy, healthy={"fast", "coding-fast"})
    res = r.handle(req(text="fix it"))
    assert res.status == 503 and spy.calls == []


def test_router_budget_429_and_hermes_night_cap(tmp_path):
    spy = Spy()
    r, _ = make(tmp_path, provider=spy, per_day=1500)
    assert r.handle(req(text="fix a")).status == 200  # 1000 spent
    assert r.handle(req(text="fix b")).status == 429  # 2000 > 1500
    assert len(spy.calls) == 1
    r2, _ = make(tmp_path / "2", provider=Spy())
    assert r2.handle(req(token="hm", text="fix a", session="h")).status == 200
    assert r2.handle(req(token="hm", text="fix b", session="h2")).status == 429  # agent cap 1500


def test_router_provider_error_502_and_not_sticky(tmp_path):
    r, _ = make(tmp_path, provider=Spy(fail=True))
    assert r.handle(req()).status == 502
    assert r.tracker.sticky("s1", "fix the bug in a.py") is None
    assert r.events.all()[0]["outcome"] == "error:provider"


def test_router_agent_role_from_system_prompt_hash(tmp_path):
    from xccode.xcroute.decide import message_hash
    sysmsg = {"role": "system", "content": "You are ContextScout."}
    r, _ = make(tmp_path, roles={message_hash("You are ContextScout."): "contextscout"})
    res = r.handle(req(text="what does a.py do?", extra=(sysmsg,)))
    assert res.event.xc_agent_role == "contextscout" and res.pool == "fast"
    res = r.handle(req(text="what does b.py do?", extra=({"role": "system", "content": "?"},),
                       session="s2"))
    assert res.event.xc_agent_role == "unknown"
