from FRS.core.position_recommend.model import Classifier, prepare
from FRS.utils.db.client import Client

import pandas as pd

MODEL_PATH = 'FRS/utils/data/position_recommend_model.pickle'


def get_data(team):
    client = Client('frs')
    db = client.db
    collection = db.player_data

    result = collection.find({'team_colors': team})
    df = pd.DataFrame(result)
    df = df.astype({'id': 'int64'})
    info_df = df[['id', 'name', 'skills', 'used']]
    df.drop(labels=['_id', 'team_colors',
            'skills', 'used'], axis=1, inplace=True)

    data, position_encoder, _ = prepare(df)
    X, y = data.drop(['id', 'name', 'positions'], axis=1), data['positions']

    return X, y, info_df, position_encoder


def recommend(team='레알 마드리드'):
    clf = Classifier()
    clf.call(MODEL_PATH)

    X, y, info_df, position_encoder = get_data(team)
    predict = clf.predict(X)
    predict = pd.Series(
        map(lambda x: position_encoder.inverse_transform(x), predict))

    predict_df = pd.DataFrame()
    predict_df[['id', 'name', 'skills', 'used']
               ] = info_df.reset_index(drop=True)
    predict_df['positions'] = y
    predict_df['predict_position'] = predict

    predict_df = predict_df.sort_values('used', ascending=False)

    print(predict_df)
    print(f'{clf.precision(predict, y)}%')
