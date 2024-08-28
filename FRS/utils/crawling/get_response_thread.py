from user_agent import generate_user_agent

import concurrent.futures
import requests
import time


def fetch_url(chunked_url):
    url, id, name = chunked_url
    headers = {
        'User-Agent': generate_user_agent(os='win', device_type='desktop'),
        'Referrer': 'https://fconline.nexon.com/'
    }

    try:
        response = requests.get(url, headers)

    except:
        return None, {'id': id, 'name': name}

    return response, id, name


def get_response(chunks):
    start = time.time()
    output, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = executor.map(fetch_url, chunks)
        for result in results:
            if result[0] == None:
                failed.append(result[1])

            else:
                output.append(result)

    end = time.time()
    return output, failed, (end - start) / 60
