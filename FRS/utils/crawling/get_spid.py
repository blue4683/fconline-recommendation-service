import json
import requests


def update_spid():
    url = 'https://open.api.nexon.com/static/fconline/meta/spid.json'
    response = requests.get(url)
    contents = response.json()

    file_path = 'FRS/utils/data/spid.json'
    with open(file_path, 'w', encoding='utf8') as f:
        json.dump(contents, f, ensure_ascii=False)
