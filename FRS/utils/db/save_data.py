import json
import os
import pymongo
import time

from client import Client
from dotenv import load_dotenv
from tqdm import tqdm


load_dotenv()

HOST = os.getenv('HOST')
PORT = os.getenv('PORT')
USERID = os.getenv('USERID')
PASSWORD = os.getenv('PASSWORD')

client = Client(HOST, PORT, USERID, PASSWORD, 'frs')
db = client.db
collection = db.player_data
collection.create_index([('id', pymongo.ASCENDING)], unique=True)

file_path = 'FRS/utils/data/player_overall_data.json'
with open(file_path, 'r', encoding='utf8') as f:
    player_data = json.load(f)

cnt = 0
start = time.time()
for id in tqdm(player_data):
    data = player_data[id]
    data['id'] = id

    try:
        collection.insert_one(data)
        cnt += 1

    except:
        print(f'{id} - 이미 저장된 선수입니다.')

end = time.time()
print(f'{cnt}개의 플레이어 데이터를 DB에 저장했습니다. {(end - start) / 60}분 소요')
