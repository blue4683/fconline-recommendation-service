import { describe, expect, it, vi } from 'vitest';
import { finalFor } from '../src/mocks/fixtures';
import { createMockTransport } from '../src/mocks/mockTransport';
import { buildRequest, runStream } from '../src/services/agentSession';
import { createLiveTransport, normalizeBaseUrl, PROPOSED_STREAM_PATH } from '../src/services/liveTransport';
import { envelope, FIXED_NOW, scriptedTransport, sse } from './helpers';

const started = (id: string, seq = 0) => sse('started', envelope(id, seq, 'started', { data_mode: 'synthetic' }));
const progress = (id: string, seq: number, stage = 'build') =>
  sse('progress', envelope(id, seq, 'progress', { stage, message: '진행 중' }));
const final = (id: string, seq: number, scenario: Parameters<typeof finalFor>[0] = 'optimal') =>
  sse('final', envelope(id, seq, 'final', finalFor(scenario, FIXED_NOW)));

async function run(script: (id: string) => string, size = 5) {
  const transport = scriptedTransport(script, size);
  const stages: string[] = [];
  const outcome = await runStream(transport, buildRequest('요청'), new AbortController().signal, {
    onProgress: (p) => stages.push(p.stage),
  });
  return { outcome, stages, transport };
}

describe('runStream', () => {
  it('정상 순서의 final을 결과로 돌려준다', async () => {
    const { outcome, stages } = await run((id) => started(id) + `: heartbeat\n\n` + progress(id, 1, 'extract') + final(id, 2));
    expect(stages).toEqual(['extract']);
    expect(outcome.kind).toBe('final');
  });

  it('error 이벤트는 서버 오류로 구분한다', async () => {
    const { outcome } = await run(
      (id) => started(id) + sse('error', envelope(id, 1, 'error', { code: 'engine_failed', message: '실패' })),
    );
    expect(outcome).toEqual({ kind: 'server_error', code: 'engine_failed', message: '실패' });
  });

  it.each([
    ['번호 누락', (id: string) => started(id) + progress(id, 2) + final(id, 3)],
    ['번호 역전', (id: string) => started(id) + progress(id, 1) + progress(id, 1) + final(id, 2)],
    ['started 없이 시작', (id: string) => progress(id, 0) + final(id, 1)],
    ['started 중복', (id: string) => started(id) + started(id, 1) + final(id, 2)],
    ['다른 요청 ID', (id: string) => started(id) + final(`${id}-old`, 1)],
    ['종료 뒤 이벤트', (id: string) => started(id) + final(id, 1) + progress(id, 2)],
    ['잘못된 JSON', (id: string) => started(id) + sse('progress', '{"broken"')],
    ['모르는 이벤트', (id: string) => started(id) + sse('token', envelope(id, 1, 'token', { text: '부분' }))],
  ])('%s → 프로토콜 오류', async (_name, script) => {
    const { outcome } = await run(script, 1000);
    expect(outcome.kind).toBe('protocol_error');
  });

  it('종료 이벤트 전에 끊기면 통신 오류이며 해 없음이 아니다', async () => {
    const { outcome } = await run((id) => started(id) + progress(id, 1));
    expect(outcome.kind).toBe('transport_error');
  });

  it('빈 줄 없이 끝난 final은 결과로 쓰지 않는다', async () => {
    const { outcome } = await run((id) => started(id) + final(id, 1).replace(/\n\n$/, '\n'));
    expect(outcome.kind).toBe('transport_error');
  });

  it('스트림 시작 전 transport 실패를 재시도하지 않는다', async () => {
    const open = vi.fn().mockRejectedValue(new Error('boom'));
    const outcome = await runStream({ mode: 'live', open }, buildRequest('요청'), new AbortController().signal);
    expect(outcome.kind).toBe('transport_error');
    expect(open).toHaveBeenCalledTimes(1);
  });

  it('모의 transport의 불규칙 청크·CRLF·여러 data 행을 실제 파서로 통과한다', async () => {
    for (const scenario of ['optimal', 'huge_numbers', 'needs_clarification', 'infeasible'] as const) {
      const transport = createMockTransport({ scenario: () => scenario, now: () => FIXED_NOW });
      const outcome = await runStream(transport, buildRequest('요청'), new AbortController().signal);
      expect(outcome.kind, scenario).toBe('final');
      if (outcome.kind === 'final') expect(outcome.result.status).toBe(scenario === 'huge_numbers' ? 'optimal' : scenario);
    }
    const transport = createMockTransport({ scenario: () => 'protocol_error', now: () => FIXED_NOW });
    expect((await runStream(transport, buildRequest('요청'), new AbortController().signal)).kind).toBe('protocol_error');
    const cut = createMockTransport({ scenario: () => 'disconnect', now: () => FIXED_NOW });
    expect((await runStream(cut, buildRequest('요청'), new AbortController().signal)).kind).toBe('transport_error');
  });
});

describe('buildRequest', () => {
  it('원문을 trim·치환하지 않고 그대로 담는다', () => {
    const message = '  리버풀\n\t4-2-3-1 😀  ';
    const req = buildRequest(message, 'fixed');
    expect(req).toEqual({ contract_version: 'web-agent.v0', request_id: 'fixed', message });
  });
});

describe('live transport', () => {
  it('base URL은 http(s)만 받고 query·인증정보가 있으면 거부한다', () => {
    expect(normalizeBaseUrl('')).toBeNull();
    expect(normalizeBaseUrl('ftp://x')).toBeNull();
    expect(normalizeBaseUrl('http://h/?k=1')).toBeNull();
    expect(normalizeBaseUrl('http://u:p@h')).toBeNull();
    expect(normalizeBaseUrl(' http://localhost:8000/api/ ')).toBe('http://localhost:8000/api');
  });

  it('원문을 본문 JSON으로만 보내고 query string에 넣지 않는다', async () => {
    const fetchImpl = vi.fn(async () =>
      new Response('', { status: 200, headers: { 'content-type': 'text/event-stream; charset=utf-8' } }),
    );
    const transport = createLiveTransport('http://localhost:8000', fetchImpl);
    const req = buildRequest('리버풀 예산 6조 BP', 'rid');
    await transport.open(req, new AbortController().signal);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`http://localhost:8000${PROPOSED_STREAM_PATH}`);
    expect(url).not.toContain('?');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual(req);
  });

  it('HTTP 비정상 응답과 스트림이 아닌 응답을 구분해 실패시키고 재시도하지 않는다', async () => {
    const http = vi.fn(async () => new Response('nope', { status: 503 }));
    const outcome = await runStream(createLiveTransport('http://h', http), buildRequest('x'), new AbortController().signal);
    expect(outcome).toMatchObject({ kind: 'transport_error', status: 503 });
    expect(http).toHaveBeenCalledTimes(1);

    const json = vi.fn(async () => new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }));
    const o2 = await runStream(createLiveTransport('http://h', json), buildRequest('x'), new AbortController().signal);
    expect(o2.kind).toBe('transport_error');
  });
});
