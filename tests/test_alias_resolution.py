"""An alias_family row follows the newest family member at every effort it offers."""
from office import candidates, config

INDEX = "Artificial Analysis Intelligence Index v4.3.2"
FAMILY = r"^gpt-(?P<version>\d+(?:\.\d+)*)-luna$"


def _concrete(model, effort, score, **extra):
    return {"model_id": model, "invocation_harness": "codex", "invocation_model_id": model, "effort": effort,
            "source_effort": effort, "invocation_source": "local-evidence: codex debug models",
            "benchmark_indexes": {INDEX: score}, **extra}


def _alias(effort="xhigh"):
    return {"model_id": "luna", "alias_family": FAMILY, "invocation_harness": "codex",
            "invocation_model_id": "gpt-6-luna", "invocation_source": "local-evidence: alias", "effort": effort,
            "effort_confidence": "mapped", "benchmark_indexes": {INDEX: 1}}


def _by_effort(rows, model="luna"):
    return {r["effort"]: r for r in rows if r["model_id"] == model}


def test_alias_is_offered_at_every_effort_of_the_newest_member():
    rows = [_alias(), _concrete("gpt-5.6-luna", "high", 20), _concrete("gpt-6-luna", "xhigh", 34),
            _concrete("gpt-6-luna", "high", 32), _concrete("gpt-6-luna", "low", 21)]
    luna = _by_effort(candidates.resolve_aliases(rows))
    assert set(luna) == {"xhigh", "high", "low"}
    assert luna["high"]["invocation_model_id"] == "gpt-6-luna"
    assert luna["high"]["benchmark_indexes"] == {INDEX: 32}
    assert luna["high"]["alias_resolved_to"] == "gpt-6-luna"
    assert luna["high"]["effort"] == "high"


def test_alias_follows_a_newer_family_member_automatically():
    rows = [_alias(), _concrete("gpt-6-luna", "high", 32), _concrete("gpt-7-luna", "high", 40),
            _concrete("gpt-7-luna", "medium", 33)]
    luna = _by_effort(candidates.resolve_aliases(rows))
    assert set(luna) == {"xhigh", "high", "medium"}
    assert luna["high"]["invocation_model_id"] == "gpt-7-luna" and luna["high"]["benchmark_indexes"] == {INDEX: 40}
    # An effort the newest member lacks keeps the alias's own static row.
    assert luna["xhigh"]["invocation_model_id"] == "gpt-6-luna" and "alias_resolved_to" not in luna["xhigh"]


def test_concrete_rows_and_unmatched_aliases_pass_through():
    rows = [_alias(), _concrete("gpt-6-luna", "high", 32)]
    assert candidates.resolve_aliases([rows[1]]) == [rows[1]]
    assert candidates.resolve_aliases([rows[0]]) == [rows[0]]


def test_shipped_catalog_offers_luna_at_high():
    luna = _by_effort(candidates.catalog_rows())
    assert luna["high"]["invocation_model_id"] == "gpt-6-luna" and luna["high"]["effort"] == "high"
    assert luna["high"]["benchmark_indexes"].get(INDEX) is not None


def test_default_reviewer_seeds_resolve_to_catalog_candidates():
    from office import routing
    roles = config.load_yaml(config.default_config_path())["roles"]
    rows = candidates.catalog_rows()
    for role in ("plan_reviewer", "code_reviewer"):
        lead = roles[role]["preferred_seed"][0]
        assert any(routing.preferred_rank(r, [lead]) == 0 for r in rows), role


def test_shipped_catalog_has_unique_model_harness_effort_rows():
    """A stale evidence-only row must not duplicate a scored routing candidate."""
    import yaml
    from office import paths

    seed = yaml.safe_load((paths.resources_root() / "catalog" / "seed.yaml").read_text(encoding="utf-8"))
    keys = [(r["model_id"], r["invocation_harness"], r["effort"]) for r in seed["models"]]
    assert len(keys) == len(set(keys)), "duplicate catalog model/harness/effort rows"

def test_alias_does_not_reenable_non_dispatchable_target():
    # Alias routing cannot launder a benchmark-only route into a runnable one.
    rows = [_alias(effort="medium"),
            _concrete("gpt-6-luna", "medium", 29, dispatchable=False)]
    [resolved] = [r for r in candidates.resolve_aliases(rows)
                  if r["model_id"] == "luna"]
    assert resolved["invocation_model_id"] == "gpt-6-luna"
    assert resolved["dispatchable"] is False


def test_haiku_alias_preserves_concrete_effort_dispatch_gate():
    [row] = [r for r in candidates.catalog_rows()
             if r["model_id"] == "haiku" and r["effort"] == "medium"]
    assert row["alias_resolved_to"] == "claude-haiku-5-5"
    assert row["dispatchable"] is False
