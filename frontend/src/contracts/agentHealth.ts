/**
 * 8001 합성 Agent 앱의 GET /health 응답(agent-health.v1, FRS/api/agent_app.py·agent_runtime.py, develop 00623ba).
 * 화면 표시용 읽기 전용 검사다. 서버 준비 여부를 브라우저가 확정하지 않으며, 최종 판단은 실제 요청의 응답이다.
 */
import { exactKeys, fail, isoTime, obj, oneOf, str } from './webAgentV0';

export const HEALTH_SCHEMA = 'agent-health.v1';

export type HealthReason = 'not_configured' | 'data_expired' | 'model_unverified';

export interface HealthModel {
  name: string;
  digest: string;
  verification: 'startup_snapshot';
  checked_at: string | null;
  ollama_version: string | null;
}

export interface HealthMaterial {
  id: string;
  batches: Record<string, string>;
  observed_at: string;
  valid_until: string;
}

export interface AgentHealth {
  status: 'ready' | 'unavailable';
  reason: HealthReason | null;
  model: HealthModel | null;
  material: HealthMaterial | null;
  busy: boolean;
}

const HEX64 = /^[0-9a-f]{64}$/;

function parseModel(value: unknown, path: string): HealthModel {
  const o = obj(value, path);
  exactKeys(o, path, ['name', 'digest', 'verification', 'checked_at', 'ollama_version']);
  const digest = str(o.digest, `${path}.digest`);
  if (!HEX64.test(digest)) fail(`${path}.digest`, '64자리 16진 digest가 필요합니다');
  return {
    name: str(o.name, `${path}.name`, { nonEmpty: true }),
    digest,
    verification: oneOf(o.verification, ['startup_snapshot'] as const, `${path}.verification`),
    checked_at: o.checked_at === null ? null : isoTime(o.checked_at, `${path}.checked_at`),
    ollama_version: o.ollama_version === null ? null : str(o.ollama_version, `${path}.ollama_version`, { nonEmpty: true }),
  };
}

function parseMaterial(value: unknown, path: string): HealthMaterial {
  const o = obj(value, path);
  exactKeys(o, path, ['id', 'batches', 'observed_at', 'valid_until']);
  const id = str(o.id, `${path}.id`);
  if (!HEX64.test(id)) fail(`${path}.id`, '64자리 16진 ID가 필요합니다');
  const b = obj(o.batches, `${path}.batches`);
  const batches: Record<string, string> = {};
  for (const [k, v] of Object.entries(b)) batches[k] = str(v, `${path}.batches.${k}`, { nonEmpty: true });
  if (Object.keys(batches).length === 0) fail(`${path}.batches`, '배치가 비어 있습니다');
  const observed_at = isoTime(o.observed_at, `${path}.observed_at`);
  const valid_until = isoTime(o.valid_until, `${path}.valid_until`);
  if (Date.parse(valid_until) <= Date.parse(observed_at)) fail(`${path}.valid_until`, '관측 시각보다 뒤여야 합니다');
  return { id, batches, observed_at, valid_until };
}

/**
 * HTTP 상태와 본문을 함께 검사한다. ready는 200이고 사유가 없어야 하며, unavailable은 503이고 사유가 있어야 한다.
 * 모르는 필드·사유·스키마 버전은 거부한다.
 */
export function parseHealth(value: unknown, httpStatus: number): AgentHealth {
  const path = 'health';
  const o = obj(value, path);
  exactKeys(o, path, ['schema_version', 'status', 'reason', 'data_mode', 'model', 'material', 'busy']);
  if (o.schema_version !== HEALTH_SCHEMA) fail(`${path}.schema_version`, `${HEALTH_SCHEMA}가 필요합니다`);
  if (o.data_mode !== 'synthetic') fail(`${path}.data_mode`, 'synthetic이 필요합니다');
  const status = oneOf(o.status, ['ready', 'unavailable'] as const, `${path}.status`);
  const reason =
    o.reason === null ? null : oneOf(o.reason, ['not_configured', 'data_expired', 'model_unverified'] as const, `${path}.reason`);
  if (typeof o.busy !== 'boolean') fail(`${path}.busy`, '참/거짓이 필요합니다');
  const model = o.model === null ? null : parseModel(o.model, `${path}.model`);
  const material = o.material === null ? null : parseMaterial(o.material, `${path}.material`);

  if (status === 'ready') {
    if (httpStatus !== 200) fail(path, 'ready는 HTTP 200이어야 합니다');
    if (reason !== null) fail(`${path}.reason`, 'ready에는 사유가 없어야 합니다');
    if (!model || model.checked_at === null || model.ollama_version === null) fail(`${path}.model`, 'ready에는 모델 확인 기록이 필요합니다');
    if (!material) fail(`${path}.material`, 'ready에는 자료 정보가 필요합니다');
  } else {
    if (httpStatus !== 503) fail(path, 'unavailable은 HTTP 503이어야 합니다');
    if (reason === null) fail(`${path}.reason`, 'unavailable에는 사유가 필요합니다');
    if (reason === 'not_configured' && (model !== null || material !== null)) fail(path, '설정이 없으면 모델·자료 정보가 없어야 합니다');
    if (reason !== 'not_configured' && (model === null || material === null)) fail(path, '모델·자료 정보가 필요합니다');
  }
  return { status, reason, model, material, busy: o.busy };
}

