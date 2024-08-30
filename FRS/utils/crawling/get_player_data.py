from FRS.utils.crawling.get_response import get_response
from get_spid import update_spid
from process_data import get_data
from parse_html import get_soup

import json
import time


def chunker(iterable):
    chunk_size = 1000
    chunks = [iterable[i:i + chunk_size]
              for i in range(0, len(iterable), chunk_size)]

    return chunks


def crawling():
    global keyword
    # 크롤링할 선수의 고유 아이디(시즌 + 선수에 해당하는 아이디)와 이름
    players_path = 'FRS/utils/data/spid.json'
    with open(players_path, 'r', encoding='utf8') as f:
        players = json.load(f)

    # 능력치 이름에 해당하는 데이터 컬럼 사전
    keyword_path = 'FRS/schema/keyword.json'
    with open(keyword_path, 'r', encoding='utf8') as f:
        keyword = json.load(f)

    while players:
        file_path = 'FRS/utils/data/player_overall_data.json'
        with open(file_path, 'r', encoding='utf8') as f:
            player_data = json.load(f)

        urls = []
        for player in players:
            id, name = player['id'], player['name']
            if player_data.get(str(id)) != None:
                continue

            url = f'https://fconline.nexon.com/DataCenter/PlayerInfo?spid={id}'
            urls.append((url, id, name))

        print(f'url {len(urls)}개 파싱 완료(중복 {len(players) - len(urls)}개)')

        pages, failed_get_response, duration = get_response(chunker(urls))
        print(
            f'page {len(pages)}개 request 완료 {len(failed_get_response)}개 실패 {duration}분 소요')

        soups, failed_get_soup, duration = get_soup(pages)
        print(
            f'soup {len(soups)}개 파싱 완료 {len(failed_get_soup)}개 실패 {duration}분 소요')

        output, failed_get_data, duration = get_data(soups)
        print(
            f'데이터 {len(output)}개 처리 완료 {len(failed_get_data)}개 실패 {duration}분 소요')

        a = time.time()
        failed = []
        for data, id, name in output:
            if data == None:
                failed.append({'id': id, 'name': name})
                continue

            player_data[id] = data
            if not len(player_data) % 1000:
                with open(file_path, 'w', encoding='utf8') as f:
                    json.dump(player_data, f, ensure_ascii=False)

                print(f'{len(player_data)}개 크롤링 완료')

        with open(file_path, 'w', encoding='utf8') as f:
            json.dump(player_data, f, ensure_ascii=False)

        b = time.time()
        print(f'데이터 {len(output)}개 저장 완료 {len(failed)} 실패 {(b - a) / 60}분 소요')

        failed.extend(failed_get_response)
        failed.extend(failed_get_soup)
        failed.extend(failed_get_data)

        if failed:
            print(f'{len(failed)}개 크롤링 실패')

        players = failed
        del output


if __name__ == '__main__':
    start = time.time()
    update_spid()
    crawling()
    end = time.time()

    print(f'전체 데이터를 크롤링 후 저장하는데 {(end - start) / 60}분이 걸렸습니다.')
