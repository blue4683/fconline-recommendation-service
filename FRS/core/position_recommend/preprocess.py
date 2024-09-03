import os
import pandas as pd

from FRS.utils.db.client import Client
from dotenv import load_dotenv

load_dotenv()

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

    exceptions = [
        '-1', '-2', '-3', '-4', '-5', '-6', '-7', '1', '2', '3', '4', '5', '6', '7', '8']
    for exception in exceptions:
        new_df = new_df[new_df['position'] != exception]

    new_df.to_csv(DATA_PATH, index=False, encoding='utf-8-sig')


def run():
    if os.path.isfile(DATA_PATH):
        print('전처리된 데이터가 존재합니다.')
        return

    client = Client('frs')
    db = client.db
    collection = db.player_data

    result = collection.find()
    make_dataframe(result)
