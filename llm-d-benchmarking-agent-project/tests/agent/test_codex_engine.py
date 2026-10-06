"""Codex adapter: real registry/approval pipeline with a hermetic SDK client."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from app.agent.codex_engine import CodexEngine
from app.agent.engine import LIVE_TURNS, steer
from app.agent.session import Session
from app.llm.codex_options import ISOLATION_CONFIG, codex_config, isolated_thread_config
from app.tools.registry import tool_definitions
from tests._helpers import _capture_ctx


def wire(data):
    return SimpleNamespace(model_dump=lambda **_: data)


class FakeCodex:
    def __init__(self, scripts, *, account='chatgpt', resume_error=None):
        self.scripts = iter(scripts)
        self.account = account
        self.resume_error = resume_error
        self.events = queue.Queue()
        self.closed = False
        self.requests = []
        self.responses = []
        self.turns = []

    def factory(self, config, approval_handler):
        self.config = config
        self.handler = approval_handler
        return self

    def start(self):
        pass

    def initialize(self):
        pass

    def close(self):
        self.closed = True
        self.events.put(RuntimeError('closed'))

    def account_read(self):
        return wire({'account': {'type': self.account}})

    def request(self, method, params, *, response_model):
        assert method == 'config/read'
        return SimpleNamespace(config={'mcp_servers': {'unrelated': {'enabled': True}}})

    def thread_start(self, params):
        self.requests.append(('start', params))
        return SimpleNamespace(thread=SimpleNamespace(id='codex-thread'))

    def thread_resume(self, thread_id, params):
        if self.resume_error:
            raise self.resume_error
        self.requests.append(('resume', thread_id, params))

    def turn_start(self, thread_id, text, params):
        self.turns.append((text, params))
        script = next(self.scripts)
        threading.Thread(target=script, args=(self,), daemon=True).start()
        return SimpleNamespace(turn=SimpleNamespace(id='turn'))

    def next_turn_notification(self, _):
        value = self.events.get(timeout=10)
        if isinstance(value, Exception):
            raise value
        return value

    def unregister_turn_notifications(self, _):
        pass

    def event(self, method, **payload):
        self.events.put(SimpleNamespace(method=method, payload=wire(payload)))

    def tool(self, name, args, tid='tool-1'):
        self.event('item/started', item={'id': tid, 'type': 'dynamicToolCall',
                                       'tool': name, 'arguments': args})
        response = self.handler('item/tool/call', {'threadId': 'codex-thread',
                                'tool': name, 'arguments': args, 'callId': tid})
        self.responses.append(response)

    def text(self, text):
        self.event('item/agentMessage/delta', delta=text)
        self.event('item/completed', item={'type': 'agentMessage', 'text': text})

    def usage(self, inp=100, cached=40, output=10):
        counts = {'inputTokens': inp, 'cachedInputTokens': cached, 'outputTokens': output}
        self.event('thread/tokenUsage/updated', tokenUsage={
            'total': counts, 'last': counts, 'modelContextWindow': 1000})

    def done(self, status='completed'):
        self.event('turn/completed', turn={'status': status, 'error': None})


def setup(tmp_path, scripts, **kwargs):
    ctx, runner = _capture_ctx(tmp_path)
    ctx.settings = ctx.settings.model_copy(update={'llm_provider': 'codex-sdk', 'simulate': False})
    session = Session(id='codex-test', ctx=ctx, catalog_injected=True)
    fake = FakeCodex(scripts, **kwargs)
    seen = []

    async def emit(kind, data):
        seen.append((kind, data))

    return session, runner, fake, seen, emit


async def decline(*_):
    return False


async def test_text_tools_usage_and_persisted_handle(tmp_path):
    def script(f):
        f.text('Reading guidance.')
        f.tool('read_knowledge', {'name': 'quickstart_playbook'})
        f.text('Use kind.')
        f.usage()
        f.done()

    s, _, fake, seen, emit = setup(tmp_path, [script])
    await CodexEngine(fake.factory).run_turn(s, 'Read guidance', emit=emit, request_approval=decline)
    assert fake.closed
    assert s.codex_thread_id == 'codex-thread' and s.sdk_session_id is None
    assert s.total_input_tokens == 60 and s.total_cache_read_tokens == 40
    assert s.total_output_tokens == 10 and s.last_context_tokens == 100
    assert fake.responses[0]['success']
    assert [kind for kind, _ in seen].index('assistant_text') < [kind for kind, _ in seen].index('tool_call')
    assert any(m['role'] == 'tool_results' for m in s.messages)
    assert seen[-1] == ('done', {})
    assert not any(k == 'error' for k, _ in seen)
    params = fake.requests[0][1]
    assert {t['name'] for t in params['dynamicTools']} == {t['name'] for t in tool_definitions()}
    assert params['environments'] == [] and params['sandbox'] == 'read-only'
    assert params['config']['mcp_servers.unrelated.enabled'] is False


async def test_declined_mutation_never_executes(tmp_path):
    def script(f):
        f.tool('run_shell', {'command': 'touch approval-marker'})
        f.done()

    s, runner, fake, seen, emit = setup(tmp_path, [script])
    gates = []

    async def reject(kind, data):
        gates.append((kind, data))
        return False

    await CodexEngine(fake.factory).run_turn(s, 'Try a write', emit=emit, request_approval=reject)
    assert len(gates) == 1 and gates[0][0] == 'command'
    assert runner.calls == []
    result = next(p['result'] for k, p in seen if k == 'tool_result')
    assert result['rejected'] is True
    assert not fake.responses[0]['success']


async def test_cancellation_reaps_parked_approval(tmp_path):
    def script(f):
        f.tool('run_shell', {'command': 'touch approval-marker'})
        f.done()

    s, runner, fake, _, emit = setup(tmp_path, [script])
    parked, released = asyncio.Event(), asyncio.Event()

    async def gate(*_):
        parked.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    task = asyncio.create_task(CodexEngine(fake.factory).run_turn(s, 'Write', emit=emit,
                                                                 request_approval=gate))
    await asyncio.wait_for(parked.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(released.wait(), 2)
    assert fake.closed and s.id not in LIVE_TURNS and not runner.calls
    assert s.ctx.current_tool_call_id is None


async def test_resume_does_not_double_count_cumulative_usage(tmp_path):
    def script(f):
        f.text('Remembered')
        f.usage(140, 50, 15)
        f.done()

    s, _, fake, _, emit = setup(tmp_path, [script])
    s.codex_thread_id = 'codex-thread'
    s.codex_token_usage = {'inputTokens': 100, 'cachedInputTokens': 40, 'outputTokens': 10}
    s.total_input_tokens, s.total_cache_read_tokens, s.total_output_tokens = 60, 40, 10
    await CodexEngine(fake.factory).run_turn(s, 'Continue', emit=emit, request_approval=decline)
    assert fake.requests[0][0] == 'resume'
    assert s.total_input_tokens == 90 and s.total_cache_read_tokens == 50
    assert s.total_output_tokens == 15


async def test_steer_runs_followup_without_losing_user_message(tmp_path):
    def first(f):
        f.text('First response')
        f.done()

    def second(f):
        f.text('Steered response')
        f.done()

    s, _, fake, seen, capture = setup(tmp_path, [first, second])

    async def emit(kind, data):
        await capture(kind, data)
        if kind == 'assistant_text' and data['text'] == 'First response':
            assert steer(s.id, 'Use my updated request')

    await CodexEngine(fake.factory).run_turn(s, 'First', emit=emit, request_approval=decline)
    assert fake.turns[1][0] == 'Use my updated request'
    assert sum(k == 'done' for k, _ in seen) == 1
    assert any(m.get('content') == 'Use my updated request' for m in s.messages)


async def test_api_key_login_is_rejected_before_inference(tmp_path):
    s, _, fake, seen, emit = setup(tmp_path, [], account='apiKey')
    await CodexEngine(fake.factory).run_turn(s, 'Hello', emit=emit, request_approval=decline)
    assert not fake.turns and not fake.requests and fake.closed
    assert any(k == 'error' and 'codex login' in p['message'] for k, p in seen)


async def test_watchdog_closes_silent_sdk(tmp_path):
    s, _, fake, seen, emit = setup(tmp_path, [lambda _: None])
    s.ctx.settings.agent_stream_watchdog_s = 0.02
    await asyncio.wait_for(CodexEngine(fake.factory).run_turn(
        s, 'Hello', emit=emit, request_approval=decline), 2)
    assert fake.closed
    assert any(k == 'error' and 'stalled' in p['message'] for k, p in seen)


def test_subscription_and_capability_isolation(tool_ctx):
    cfg = codex_config(tool_ctx.settings)
    assert cfg.env['OPENAI_API_KEY'] == cfg.env['CODEX_API_KEY'] == ''
    assert 'forced_login_method="chatgpt"' in cfg.config_overrides
    fake = FakeCodex([])
    isolated = isolated_thread_config(fake)
    assert isolated['mcp_servers.unrelated.enabled'] is False
    assert all(not ISOLATION_CONFIG[f'features.{name}']
               for name in ('apps', 'plugins', 'shell_tool', 'multi_agent', 'browser_use'))


async def test_missing_thread_replays_prior_messages_once(tmp_path):
    from openai_codex import InvalidRequestError

    def script(f):
        f.text('Recovered')
        f.done()

    s, _, fake, seen, emit = setup(tmp_path, [script],
                                  resume_error=InvalidRequestError(-32600, 'thread not found'))
    s.codex_thread_id = 'gone'
    s.messages.append({'role': 'user', 'content': 'Keep this earlier request'})
    await CodexEngine(fake.factory).run_turn(s, 'Continue', emit=emit, request_approval=decline)
    assert fake.requests[0][0] == 'start'
    assert fake.turns[0][0].count('Keep this earlier request') == 1
    assert not any(k == 'error' for k, _ in seen)


async def test_resume_auth_failure_does_not_retry_or_replay(tmp_path):
    from openai_codex import InvalidRequestError

    s, _, fake, seen, emit = setup(tmp_path, [],
                                  resume_error=InvalidRequestError(-32600, 'authentication failed'))
    s.codex_thread_id = 'saved'
    await CodexEngine(fake.factory).run_turn(s, 'Continue', emit=emit, request_approval=decline)
    assert not fake.requests and not fake.turns
    assert any(k == 'error' for k, _ in seen)


async def test_codex_thread_and_usage_survive_disk_reload(tmp_path):
    from app.agent.session import SessionManager

    def script(f):
        f.text('Saved')
        f.usage()
        f.done()

    s, _, fake, _, emit = setup(tmp_path, [script])
    await CodexEngine(fake.factory).run_turn(s, 'Remember this', emit=emit, request_approval=decline)
    state = json.loads((s.ctx.workspace / 'state.json').read_text())
    assert state['codex_thread_id'] == 'codex-thread'
    assert state['codex_token_usage']['inputTokens'] == 100
    settings = s.ctx.settings.model_copy(update={'workspace_dir': tmp_path / 'reload'})
    manager = SessionManager(settings, s.ctx.policy, s.ctx.runner)
    saved_dir = settings.resolved_workspace_dir / 'sessions' / s.id
    saved_dir.mkdir(parents=True)
    (saved_dir / 'state.json').write_text(json.dumps(state))
    restored = manager.get_or_load(s.id)
    assert restored.codex_thread_id == s.codex_thread_id
    assert restored.codex_token_usage == s.codex_token_usage
    assert restored.sdk_session_id is None


async def test_approved_mutation_uses_existing_runner(tmp_path):
    def script(f):
        f.tool('run_shell', {'command': 'touch approved-marker'})
        f.done()

    s, runner, fake, seen, emit = setup(tmp_path, [script])
    gates = []

    async def approve(kind, payload):
        gates.append(kind)
        return True

    await CodexEngine(fake.factory).run_turn(s, 'Write', emit=emit, request_approval=approve)
    assert gates == ['command']
    assert len(runner.calls) == 1
    assert runner.calls[0]['argv'] == ['bash', '-lc', 'touch approved-marker']
    assert fake.responses[0]['success']
    assert not any(k == 'error' for k, _ in seen)


async def test_native_execution_requests_are_declined(tmp_path):
    def script(f):
        assert f.handler('item/commandExecution/requestApproval', {}) == {'decision': 'decline'}
        assert f.handler('item/fileChange/requestApproval', {}) == {'decision': 'decline'}
        assert f.handler('item/permissions/requestApproval', {}) == {'permissions': {}, 'scope': 'turn'}
        response = f.handler('item/tool/call', {'threadId': 'codex-thread',
                                              'tool': 'not_registered', 'callId': 'bad',
                                              'arguments': {}})
        assert not response['success']
        f.done()

    s, runner, fake, _, emit = setup(tmp_path, [script])
    await CodexEngine(fake.factory).run_turn(s, 'Probe', emit=emit, request_approval=decline)
    assert not runner.calls
