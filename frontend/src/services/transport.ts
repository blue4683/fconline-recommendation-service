import type { StreamRequest } from '../contracts/webAgentV1';
import type { StreamRequestV2 } from '../contracts/webAgentV2';

/** v1·v2 요청. 응답 envelope는 요청과 같은 계약 버전이어야 한다. */
export type AnyStreamRequest = StreamRequest | StreamRequestV2;

export type TransportMode = 'mock' | 'live';

/** 응답 본문을 열기 전 단계의 실패. 스트림 시작 뒤 오류와 구분한다. */
export class TransportError extends Error {
  constructor(
    readonly kind: 'network' | 'http' | 'content_type' | 'no_body',
    message: string,
    readonly status?: number,
    /** 스트림 전 거절 본문의 detail.code. 409에서만 읽으며 화면 안내 구분에만 쓴다. */
    readonly code?: string,
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
  open(request: AnyStreamRequest, signal: AbortSignal): Promise<ReadableStream<Uint8Array>>;
}
