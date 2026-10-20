import json
from pathlib import Path

from FRS.utils.crawling.get_response import CollectionStopped, fetch_url


def update_spid():
    url = "https://open.api.nexon.com/static/fconline/meta/spid.json"
    result = fetch_url((url, None, "spid"))
    if result[0] is None:
        raise CollectionStopped(result[1])
    response = result[0]
    try:
        contents = response.json()
    finally:
        response.close()
    file_path = Path(__file__).resolve().parents[1] / "data/spid.json"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as destination:
        json.dump(contents, destination, ensure_ascii=False)
