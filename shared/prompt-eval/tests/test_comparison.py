import pytest
from test_runner import Adapter, read, setup


def envelope(tmp_path, config):
    from prompt_eval.runner import run

    r, s = setup(tmp_path, config)
    return read(s, run(config, {"extractor": Adapter()}, s, r))


def test_comparison_preserves_stage_metrics_and_repetition_ranges(tmp_path, config):
    from prompt_eval.comparison import compare

    env = envelope(tmp_path, config)
    result = compare([env, env.model_copy(update={"run_id": "second"})])
    assert result["stages"]["extractor"][0]["metrics"]["quality"] == {
        "mean": 1.5,
        "min": 1.0,
        "max": 2.0,
        "n": 2,
    }


@pytest.mark.parametrize(
    "change", ["repetitions", "dataset", "model", "production", "cases", "stage", "partial"]
)
def test_comparison_rejects_incompatible_or_incomplete_runs(tmp_path, config, change):
    from prompt_eval.comparison import ComparisonError, compare
    from prompt_eval.security import digest

    env = envelope(tmp_path, config)
    cfg = config.model_dump()
    stages = env.stages
    if change == "repetitions":
        cfg["repetitions"] = 3
    if change == "dataset":
        cfg["dataset"]["digest"] = "c" * 64
    if change == "production":
        cfg["production_behavior"]["digest"] = "c" * 64
    if change == "model":
        cfg["model"]["model"] = "different"
    if change == "cases":
        cfg["cases"][0]["source_digests"]["source"] = "c" * 64
    if change == "stage":
        cfg["stages"][0]["evaluator"]["digest"] = "c" * 64
    if change == "partial":
        stages = (env.stages[0].model_copy(update={"repetitions": env.stages[0].repetitions[:1]}),)
    from prompt_eval.models import RunConfig

    cfg = RunConfig.model_validate(cfg)
    other = env.model_copy(
        update={"config": cfg, "config_digest": digest(cfg), "stages": stages, "run_id": "other"}
    )
    with pytest.raises(ComparisonError):
        compare([env, other])


def test_integrity_retrieval_detects_modified_child_artifact(tmp_path, config):
    from pathlib import Path

    from prompt_eval.retrieval import retrieve
    from prompt_eval.runner import run
    from prompt_eval.storage import PersistenceError

    r, s = setup(tmp_path, config)
    receipt = run(config, {"extractor": Adapter()}, s, r)
    assert retrieve(receipt).status == "complete"
    Path(s.path / "config.json").write_text("{}")
    with pytest.raises(PersistenceError):
        retrieve(receipt)


def test_release_rejects_development_runs(tmp_path, config):
    from prompt_eval.comparison import ComparisonError, make_release

    env = envelope(tmp_path, config)
    with pytest.raises(ComparisonError):
        make_release(env, env)

@pytest.mark.parametrize("official", [False, True])
def test_comparison_rejects_different_usable_case_masks(tmp_path, config, official):
    from prompt_eval.comparison import ComparisonError, compare, make_release
    from prompt_eval.models import CaseResult, RunConfig
    from prompt_eval.runner import run

    data = config.model_dump(mode="json")
    data["cases"].append({**data["cases"][0], "case_id": "two", "source_digests": {"source": "c" * 64}})
    cfg = RunConfig.model_validate(data)

    class Subset(Adapter):
        def __init__(self, usable_id):
            self.usable_id = usable_id

        def execute(self, case, repetition, capture):
            return CaseResult(case_id=case.case_id, usable=case.case_id == self.usable_id,
                              metrics={"quality": 1})

    envs = []
    for i, usable_id in enumerate(("one", "two")):
        variant = ("baseline", "tuned")[i] if official else "development"
        current = cfg.model_copy(update={"variant": variant})
        registry, store = setup(tmp_path / str(i), current, official=official)
        envs.append(read(store, run(current, {"extractor": Subset(usable_id)}, store, registry)).model_copy(update={"run_id": str(i)}))
    assert compare([envs[0], envs[0].model_copy(update={"run_id": "matching"})])["stages"]
    with pytest.raises(ComparisonError, match="usable"):
        compare(envs)
    if official:
        with pytest.raises(ComparisonError, match="usable"):
            make_release(*envs)


def test_comparison_accepts_matching_usable_masks(tmp_path, config):
    from prompt_eval.comparison import compare
    env = envelope(tmp_path, config)
    assert compare([env, env.model_copy(update={"run_id": "other"})])["stages"]


def test_comparison_rejects_different_aggregate_metric_keys(tmp_path, config):
    from prompt_eval.comparison import ComparisonError, compare
    from prompt_eval.models import StageSummary

    env = envelope(tmp_path, config)
    other = env.model_copy(deep=True, update={"run_id": "other"})
    other = other.model_copy(update={"stages": (other.stages[0].model_copy(update={"aggregate": StageSummary(metrics={"different": 1})}),)})
    with pytest.raises(ComparisonError, match="aggregate"):
        compare([env, other])
