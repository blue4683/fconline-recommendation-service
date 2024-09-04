from FRS.core.position_recommend.model import Classifier, prepare
from FRS.utils.db.client import Client

import pandas as pd

MODEL_PATH = 'FRS/utils/data/position_recommend_model.pickle'


def get_data(id):
    client = Client('frs')
    db = client.db
    collection = db.player_data

    result = collection.find({'id': {'$in': id}})
    df = pd.DataFrame(result)
    df.drop(labels=['_id', 'team_colors',
            'skills', 'used'], axis=1, inplace=True)

    df = df.astype({'id': 'int64'})
    info_df = df[['id', 'name']]
    data, position_encoder, _ = prepare(df)
    X, y = data.drop(['id', 'name', 'positions'], axis=1), data['positions']

    return X, y, info_df, position_encoder


def recommend(id=['100214100', '100000250', '804270673']):
    clf = Classifier()
    clf.call(MODEL_PATH)

    X, y, info_df, position_encoder = get_data(id)
    predict = clf.predict(X)
    predict = pd.Series(
        map(lambda x: position_encoder.inverse_transform(x), predict))

    predict_df = pd.DataFrame()
    predict_df[['id', 'name']] = info_df.reset_index(drop=True)
    predict_df['positions'] = y
    predict_df['predict_position'] = predict

    print(predict_df)
    print(f'{clf.precision(predict, y)}%')
