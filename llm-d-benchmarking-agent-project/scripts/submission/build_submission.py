#!/usr/bin/env python3
"""Build Part 2 from current owned source and pinned upstreams, never from old src/.

Only Git-listed source is copied. Runtime data, credentials, Git internals, and private
rehearsal notes are excluded. Safe upstream symlinks are materialized for ZIP portability.
"""
import hashlib
import io
import json
import posixpath
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROJECT = ROOT / "llm-d-benchmarking-agent-project"
PACKAGE = ROOT / "final_submission"
REPOS = ('.', 'llm-d-bench-mcp', 'llm-d', 'llm-d-benchmark', 'llm-d-skills')
REQUIRED = ('students.json', 'repo.url', 'presentation.html', 'presentation.pdf',
            'docs/user_manual.md', 'docs/dev_guide.md', 'docs/architecture.svg',
            'docs/architecture.html', 'docs/verification.md', 'docs/third_party.md')


def prepare():
    """Refresh all deliverables from their canonical repository sources."""
    (PACKAGE / 'docs').mkdir(parents=True, exist_ok=True)
    sources = {
        'students.json': 'scripts/submission/students.json',
        'docs/user_manual.md': 'docs/guides/SUBMISSION_USER_MANUAL.md',
        'docs/dev_guide.md': 'docs/reference/DEVELOPER_GUIDE.md',
        'docs/architecture.svg': 'docs/reference/architecture.svg',
        'docs/architecture.html': 'docs/reference/architecture.html',
        'docs/verification.md': 'docs/submission/verification.md',
        'docs/third_party.md': 'docs/submission/third_party.md',
    }
    for destination, source in sources.items():
        shutil.copyfile(PROJECT / source, PACKAGE / destination)
    guide = PACKAGE / 'docs/dev_guide.md'
    guide.write_text(guide.read_text().replace('(ARCHITECTURE.md)',
        '(../src/llm-d-benchmarking-agent-project/docs/reference/ARCHITECTURE.md)'))
    (PACKAGE / 'repo.url').write_text('https://github.com/TalBenAmii/llm-d-benchmarking-agent\n')
    html = (PROJECT / 'docs/submission/presentation.html').read_text()
    html = html.replace('../reference/architecture.svg', 'docs/architecture.svg')
    html = html.replace('../demo/', 'src/llm-d-benchmarking-agent-project/docs/demo/')
    html = html.replace('../images/', 'src/llm-d-benchmarking-agent-project/docs/images/')
    (PACKAGE / 'presentation.html').write_text(html)
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        raise SystemExit('Google Chrome or Chromium is required to render the A4 PDF')
    import tempfile
    with tempfile.TemporaryDirectory(prefix='submission-print-') as profile:
        subprocess.run([chrome, '--headless', '--no-sandbox', '--disable-gpu',
            '--no-pdf-header-footer', '--user-data-dir=' + profile,
            '--print-to-pdf=' + str(PACKAGE / 'presentation.pdf'),
            (PACKAGE / 'presentation.html').as_uri()], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=90)
    (PACKAGE / 'README.md').write_text(
        '# Final submission\n\n'
        '- Part 1: **presentation.pdf** (A4; title/name-only cover).\n'
        '- Part 2: **project_submission.zip** (required root layout).\n\n'
        'Start with docs/user_manual.md after extracting the ZIP. '
        'See docs/verification.md for checks and limitations.\n'
        'Sources and presentation are maintained in the repository; rebuild with '
        '`python3 llm-d-benchmarking-agent-project/scripts/submission/build_submission.py`.\n')



def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def main():
    prepare()
    for name in REQUIRED:
        if not (PACKAGE / name).is_file():
            raise SystemExit(f'Missing required artifact: {name}')
    data = json.loads((PACKAGE / 'students.json').read_text())
    assert data['course'] == '0230371' and data['semester'] == 'spring 2026'
    assert data['project_name'] and data['students']
    assert all(s['name'] and type(s['id']) is int and s['id'] > 0 for s in data['students'])
    entries, revisions = {}, []
    forbidden = {'.git', '.venv', 'node_modules', '__pycache__', 'workspace', 'private_notes', 'final_submission', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
    dockerfile = (ROOT / 'llm-d-benchmarking-agent-project/Dockerfile').read_text()
    pins = {name: re.search(r'^ARG ' + arg + r'=(.+)$', dockerfile, re.M).group(1)
            for name, arg in [('llm-d', 'LLMD_REF'), ('llm-d-benchmark', 'BENCH_REF'),
                              ('llm-d-skills', 'SKILLS_REF')]}
    for name in REPOS:
        repo = (ROOT / name).resolve()
        if git(repo, 'diff', '--name-only', '--diff-filter=U').strip():
            raise SystemExit(f'Unresolved conflicts: {name}')
        if name in pins:
            ref = pins[name]
            revisions.append({
                'directory': 'src/' + name,
                'repository': git(repo, 'remote', 'get-url', 'origin').decode().strip(),
                'commit': git(repo, 'rev-parse', ref + '^{commit}').decode().strip(),
                'ref': ref, 'selection': 'Dockerfile compatibility pin',
                'updated_workspace_head': git(repo, 'rev-parse', 'HEAD').decode().strip(),
                'working_tree_changes': [],
            })
            with tarfile.open(fileobj=io.BytesIO(git(repo, 'archive', ref)), mode='r:') as tar:
                for member in tar.getmembers():
                    if member.isdir():
                        continue
                    if not (member.isfile() or member.issym() or member.islnk()):
                        raise SystemExit(f'Unsupported archive member: {name}/{member.name}')
                    if Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                        raise SystemExit('Unsafe upstream archive path')
                    stream = tar.extractfile(member)
                    if stream is None and member.issym():
                        prefix = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname)) + '/'
                        if prefix.startswith('../'):
                            raise SystemExit('Upstream symlink escapes archive')
                        matches = [m for m in tar.getmembers() if m.isfile() and m.name.startswith(prefix)]
                        if not matches:
                            raise SystemExit(f'Unresolved directory symlink: {member.name}')
                        for child in matches:
                            child_name = member.name + '/' + child.name.removeprefix(prefix)
                            entries['src/' + name + '/' + child_name] = (
                                tar.extractfile(child).read(), 0o755 if child.mode & 0o111 else 0o644,
                                member.linkname + '/' + child.name.removeprefix(prefix))
                        continue
                    if stream is None:
                        raise SystemExit(f'Unreadable archive member: {member.name}')
                    mode = 0o755 if member.mode & 0o111 else 0o644
                    entries['src/' + name + '/' + member.name] = (
                        stream.read(), mode, member.linkname if member.issym() or member.islnk() else None)
            continue
        rows = git(repo, 'ls-files', '--stage', '-z').split(b'\0')
        tracked = {}
        for row in filter(None, rows):
            meta, raw = row.split(b'\t', 1)
            mode, _, stage = meta.decode().split()
            assert stage == '0'
            tracked[raw.decode()] = mode
        # Include new owned source files, but not untracked material from upstream repos.
        if name in ('.', 'llm-d-bench-mcp'):
            for raw in filter(None, git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0')):
                path = raw.decode()
                tracked[path] = '100755' if (repo / path).stat().st_mode & 0o111 else '100644'
        revisions.append({
            'directory': 'src' if name == '.' else 'src/' + name,
            'repository': git(repo, 'remote', 'get-url', 'origin').decode().strip(),
            'commit': git(repo, 'rev-parse', 'HEAD').decode().strip(),
            'working_tree_changes': git(repo, 'status', '--short').decode().splitlines(),
        })
        for relative, mode in tracked.items():
            path = Path(relative)
            if forbidden.intersection(path.parts) or (path.name.startswith('.env') and path.name != '.env.example'):
                raise SystemExit(f'Refusing runtime/secret path: {name}/{relative}')
            source = repo / path
            if mode not in ('100644', '100755', '120000') or not source.is_file():
                raise SystemExit(f'Unsupported source: {name}/{relative} ({mode})')
            if not source.resolve().is_relative_to(repo):
                raise SystemExit(f'Source escapes repository: {name}/{relative}')
            target_name = 'src/' + ('' if name == '.' else name + '/') + relative
            content = source.read_bytes()
            unix_mode = 0o755 if mode == '100755' or (mode == '120000' and source.stat().st_mode & 0o111) else 0o644
            entries[target_name] = (content, unix_mode, source.readlink().as_posix() if mode == '120000' else None)
    stage = PACKAGE / '.src-build'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    for name, (content, mode, _) in entries.items():
        dest = stage / name.removeprefix('src/')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(mode)
    src = PACKAGE / 'src'
    if src.is_symlink():
        raise SystemExit('Refusing symlinked src/')
    if src.exists():
        shutil.rmtree(src)
    stage.rename(src)
    manifest = {'repositories': revisions, 'files': [
        {'path': n, 'sha256': hashlib.sha256(b).hexdigest(), 'mode': oct(m),
         **({'materialized_symlink': link} if link else {})}
        for n, (b, m, link) in sorted(entries.items())
    ]}
    (PACKAGE / 'docs/source_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for file in [*(PACKAGE / n for n in REQUIRED), PACKAGE / 'docs/source_manifest.json']:
        entries[file.relative_to(PACKAGE).as_posix()] = (file.read_bytes(), 0o644, None)
    archive = PACKAGE / 'project_submission.zip'
    temporary = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as output:
        for name, (content, mode, _) in sorted(entries.items()):
            item = zipfile.ZipInfo(name)
            item.create_system = 3
            item.external_attr = (0o100000 | mode) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(item, content)
    with zipfile.ZipFile(temporary) as check:
        assert check.testzip() is None
        assert all(name in check.namelist() for name in REQUIRED)
        assert all(check.read(row['path']) == (PACKAGE / row['path']).read_bytes() for row in manifest['files'])
    temporary.replace(archive)
    print(f'Packaged {len(manifest["files"])} source files from {len(REPOS)} repositories; {archive.stat().st_size:,} bytes.')


if __name__ == '__main__':
    main()
