#!/usr/bin/env python3
"""Extract and verify the course ZIP without the original checkout or its Python venv.

Uses the user/developer manual's install commands. Never calls a live model or deploys.
Logs and extracted runtime data stay in --destination, which must not already exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    extracted = destination / 'submission'
    logs = destination / 'logs'
    logs.mkdir()
    archive = args.archive.resolve()
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert bundle.testzip() is None
        assert len(names) == len(set(names)), 'Duplicate archive paths'
        assert {n.split('/')[0] for n in names} == {
            'src', 'docs', 'repo.url', 'students.json', 'presentation.html', 'presentation.pdf',
        }
        for name in names:
            path = Path(name)
            assert not path.is_absolute() and '..' not in path.parts, name
            assert not {'.git', '.venv', '__pycache__', 'node_modules', 'workspace'}.intersection(path.parts), name
            assert not (path.name.startswith('.env') and path.name != '.env.example'), name
        students = json.loads(bundle.read('students.json'))
        assert students['course'] == '0230371' and students['semester'] == 'spring 2026'
        assert all(s['name'] and type(s['id']) is int for s in students['students'])
        assert bundle.read('repo.url').decode().strip().startswith('https://github.com/')
        manifest = json.loads(bundle.read('docs/source_manifest.json'))
        assert {r['path'] for r in manifest['files']} == {n for n in names if n.startswith('src/')}
        for row in manifest['files']:
            assert hashlib.sha256(bundle.read(row['path'])).hexdigest() == row['sha256'], row['path']
        bundle.extractall(extracted)
        for member in bundle.infolist():
            (extracted / member.filename).chmod((member.external_attr >> 16) & 0o777 or 0o644)
    project = extracted / 'src/llm-d-benchmarking-agent-project'
    env = os.environ.copy()
    for key in ('VIRTUAL_ENV', 'PYTHONPATH', 'REPOS_DIR', 'WORKSPACE_DIR', 'UV_PROJECT_ENVIRONMENT',
                'LLM_EVAL_LIVE', 'BUGHUNT', 'CLUSTER_SERVICE_E2E', 'LOCAL_CLUSTER_E2E',
                'SDK_ENGINE_LIVE', 'LLMD_SIM_INTEGRATION', 'FUZZ_SOAK', 'LLM_EVAL_SIMULATE'):
        env.pop(key, None)
    env.update(REPOS_DIR=str(extracted / 'src'), LLM_PROVIDER='claude-agent-sdk', SIMULATE='1')
    checks = []

    def run(label: str, command: list[str], cwd: Path = project, timeout: int = 600) -> None:
        print(f'{label}: {" ".join(command)}', flush=True)
        with (logs / f'{label}.log').open('w') as log:
            process = subprocess.run(command, cwd=cwd, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, timeout=timeout, check=False)
        checks.append({'check': label, 'command': command, 'returncode': process.returncode})
        (destination / 'results.json').write_text(json.dumps({
            'archive_sha256': archive_hash, 'source_files': len(manifest['files']),
            'destination': str(destination), 'checks': checks,
        }, indent=2) + '\n')
        if process.returncode:
            raise SystemExit(f'{label} failed: see {logs / (label + ".log")}')
        print(f'{label}: PASS', flush=True)

    run('install', ['uv', 'sync', '--locked', '--extra', 'dev', '--python', '3.11'])
    (project / '.env').write_bytes((project / '.env.example').read_bytes())
    python = str(project / '.venv/bin/python')
    run('python', [python, '-c', 'import sys; print(sys.version); print(sys.prefix); assert sys.prefix != sys.base_prefix'])
    run('tests', [python, '-m', 'pytest', 'tests/', '-n', '4', '--cov=app', '--cov-fail-under=85', '-ra'])
    run('quality', ['make', 'lint', 'typecheck'])
    run('flows', [python, 'scripts/eval/validate_flows.py'])
    run('benchmark-venv', ['uv', 'venv', '--python', '3.11', '../llm-d-benchmark/.venv'])
    run('benchmark-install', ['uv', 'pip', 'install', '--python', '../llm-d-benchmark/.venv/bin/python',
                             '-r', 'scripts/submission/benchmark-requirements.txt',
                             '-e', '../llm-d-benchmark/benchmark-report', '-e', '../llm-d-benchmark'])
    run('benchmark-cli', ['../llm-d-benchmark/.venv/bin/llmdbenchmark', '--help'])
    run('benchmark-bridge', [python, '-m', 'pytest', 'tests/tools/test_aggregate_runs.py', '-q', '-ra'])
    run('mcp-install', ['uv', 'pip', 'install', '--python', '.venv/bin/python', '-e', '../llm-d-bench-mcp[dev]'])
    run('mcp-tests', [python, '-m', 'pytest', 'tests/', '-q'], extracted / 'src/llm-d-bench-mcp')
    run('smoke', [python, 'scripts/submission/smoke_submission.py'])
    print(f'Verified archive {archive_hash}; evidence: {destination}', flush=True)


if __name__ == '__main__':
    main()
