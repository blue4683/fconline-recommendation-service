from bs4 import BeautifulSoup

import multiprocessing
import time


def parse_html(page):
    response, id, name = page
    if response == None:
        return None, {'id': id, 'name': name}

    soup = BeautifulSoup(response.text, 'html.parser')

    return soup, id, name


def get_soup(pages):
    start = time.time()
    output, failed = [], []
    with multiprocessing.Pool(multiprocessing.cpu_count()) as pool:
        results = pool.imap(parse_html, pages)
        for result in results:
            if result[0] == None:
                failed.append(result[1])

            else:
                output.append(result)

    end = time.time()
    return output, failed, (end - start) / 60
