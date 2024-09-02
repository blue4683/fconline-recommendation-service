import os
import pandas as pd

from FRS.utils.db.client import Client
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv('HOST')
PORT = os.getenv('PORT')
USERID = os.getenv('USERID')
PASSWORD = os.getenv('PASSWORD')
DATA_PATH = 'FRS/utils/data/position_recommend_data.csv'


def make_dataframe(data):
    df = pd.DataFrame(data)
    df.drop(labels=['_id', 'team_colors',
            'skills', 'used'], axis=1, inplace=True)

    positions = df['positions'].apply(lambda x: pd.Series(x))
    positions = positions.stack().reset_index(
        level=1, drop=True).to_frame('position')
    new_df = df.merge(positions, left_index=True, right_index=True, how='left')
    new_df.drop(labels='positions', axis=1, inplace=True)

    new_df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')


def run():
    if os.path.isfile(DATA_PATH):
        print('전처리된 데이터가 존재합니다.')
        return

    client = Client(HOST, PORT, USERID, PASSWORD, 'frs')
    db = client.db
    collection = db.player_data

    result = collection.find()
    make_dataframe(result)
