"""추출 결과 JSON의 중복 키와 비유한 값을 거부한다. 보정/I/O는 없다."""

import json


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def strict_json(text):
    def reject(value):
        raise ValueError("non_finite")

    return json.loads(text, object_pairs_hook=_pairs, parse_constant=reject)
