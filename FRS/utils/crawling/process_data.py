import concurrent.futures
import json
import time

# 능력치 이름에 해당하는 데이터 컬럼 사전
keyword_path = 'FRS/schema/keyword.json'
with open(keyword_path, 'r', encoding='utf8') as f:
    data_keyword = json.load(f)


def process_data(player_data):
    soup, id, name = player_data
    if soup == None:
        return None, {'id': id, 'name': name}

    try:
        # 선수 능력치를 저장할 데이터 선언
        data_path = 'FRS/schema/player_info.json'
        with open(data_path, 'r', encoding='utf8') as f:
            data = json.load(f)

        data['name'] = name

        info_ab_div = soup.select_one(
            '#middle > div > div > div:nth-child(2) > div.content.data_detail > div > div.content_header > div.info_wrap > div.info_line.info_ab')

        position_spans = info_ab_div.select('span')
        for position_span in position_spans:
            position = position_span.select_one('span')
            if not position:
                continue

            data['positions'].append(position.get_text())

        info_etc_div = soup.select_one(
            '#middle > div > div > div:nth-child(2) > div.content.data_detail > div > div.content_header > div.info_wrap > div.info_line.info_etc')

        height = info_etc_div.select_one('span.etc.height')
        data['height'] = int(height.get_text()[:-2])

        weight = info_etc_div.select_one('span.etc.weight')
        data['weight'] = int(weight.get_text()[:-2])

        physical = info_etc_div.select_one('span.etc.physical')
        data['physical'] = physical.get_text()

        dribble_skill = info_etc_div.select_one('span.etc.skill')
        data['dribble_skill'] = dribble_skill.get_text().count('★')

        foot = info_etc_div.select_one('span.etc.foot')
        foot = foot.get_text().split()
        left, right = int(foot[0][1:]), int(foot[-1][1:])
        data['left_foot'] = left
        data['right_foot'] = right

        nation = soup.select_one(
            '#middle > div > div > div:nth-child(2) > div.content.data_detail > div > div.content_header > div.info_wrap > div.info_line.info_team > div.etc.nation > span.txt')
        data['team_colors'].append(nation.get_text())

        skills = soup.select_one(
            '#middle > div > div > div:nth-child(2) > div.content.data_detail > div > div.content_header > div.info_wrap > div.skill_wrap')

        skill_spans = skills.select('span')
        for skill_span in skill_spans:
            skill = skill_span.select_one('span')
            if not skill:
                continue

            data['skills'].append(skill.get_text())

        ovr_div = soup.select_one(
            '#middle > div > div > div:nth-child(2) > div.content.data_detail > div > div.content_bottom')

        ovr_ul = ovr_div.select('ul')
        for ovr_li in ovr_ul:
            for ovr in ovr_li:
                ovr_list = ovr.get_text().split()
                if not ovr_list:
                    continue

                ovr_name = ' '.join(ovr_list[:-1])
                if ovr_name not in data_keyword:
                    continue

                data[data_keyword[ovr_name]] = int(ovr_list[-1])

        ranker_record = soup.select(
            '#middle > div > div > div.ranker_record > div.content > div > div.tbody > div > span')

        try:
            data['used'] = int(ranker_record[0].get_text())

        except:
            pass

        clubs = soup.select(
            '#middle > div > div > div.view_wrap > div:nth-child(1) > div.content.data_detail_club > div.data_table > ul > li')

        player_club = set()
        for club in clubs:
            club_div = club.select('div')
            player_club.add(club_div[1].get_text())

        data['team_colors'] = list(player_club)

    except:
        return None, {'id': id, 'name': name}

    return data, id, name


def get_data(soups):
    start = time.time()
    output, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        results = executor.map(process_data, soups)
        for result in results:
            if result[0] == None:
                failed.append(result[1])

            else:
                output.append(result)

    end = time.time()
    return output, failed, (end - start) / 60
