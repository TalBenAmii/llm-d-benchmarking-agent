"""Report schema discovery survives upstream package relocation without vendoring."""
from pathlib import Path

import pytest

from app.config import Settings
from app.paths import benchmark_report_dir


@pytest.mark.parametrize("layout", [
    "benchmark-report/llmd_benchmark_report",
    "llmdbenchmark/analysis/benchmark_report",
])
def test_report_schema_layout(tmp_path, layout):
    repo = tmp_path / "llm-d-benchmark"
    directory = repo / layout
    directory.mkdir(parents=True)
    schema = directory / "br_v0_2_json_schema.json"
    schema.write_text('{"type": "object"}')
    assert Settings(repos_dir=tmp_path).benchmark_report_schema_path == schema


def test_current_schema_wins_and_missing_is_not_hidden(tmp_path):
    current = tmp_path / "benchmark-report/llmd_benchmark_report"
    legacy = tmp_path / "llmdbenchmark/analysis/benchmark_report"
    assert benchmark_report_dir(tmp_path) == current
    for directory in (current, legacy):
        directory.mkdir(parents=True)
        (directory / "br_v0_2_json_schema.json").write_text("{}")
    assert benchmark_report_dir(Path(tmp_path)) == current
