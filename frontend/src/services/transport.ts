import type { StreamRequest } from '../contracts/webAgentV0';

export type TransportMode = 'mock' | 'live';

/** 응답 본문을 열기 전 단계의 실패. 스트림 시작 뒤 오류와 구분한다. */
export class TransportError extends Error {
  constructor(
    readonly kind: 'network' | 'http' | 'content_type' | 'no_body',
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = 'TransportError';
  }
}

/**
 * 요청 하나를 보내고 text/event-stream 본문을 연다.
 * 구현은 자동 재연결·자동 재전송을 하지 않는다. signal 중단은 이 클라이언트의 읽기만 멈추며
 * 서버 계산 취소를 보장하지 않는다.
 */
export interface Transport {
  readonly mode: TransportMode;
  open(request: StreamRequest, signal: AbortSignal): Promise<ReadableStream<Uint8Array>>;
}
