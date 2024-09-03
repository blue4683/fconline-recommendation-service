from FRS.core.position_recommend.model import Classifier
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler


import os
import pandas as pd
import pickle
import FRS.core.position_recommend.preprocess as preprocess

DATA_PATH = 'FRS/utils/data/position_recommend_data.csv'
FORM_ENCODER_PATH = 'FRS/utils/data/form_encoder.pickle'
POSITION_ENCODER_PATH = 'FRS/utils/data/position_encoder.pickle'
MODEL_PATH = 'FRS/utils/data/position_recommend_model.pickle'
SCALER_PATH = 'FRS/utils/data/position_recommend_scaler.pickle'


def scale_dataset(data):
    if os.path.isfile(SCALER_PATH):
        with open(SCALER_PATH, 'rb') as f:
            scaler = pickle.load(f)

        scaled_data = scaler.transform(data)

    else:
        scaler = StandardScaler()
        scaled_data = scaler.fit_transform(data)
        with open(SCALER_PATH, 'wb') as f:
            pickle.dump(scaler, f, pickle.HIGHEST_PROTOCOL)

    data = pd.DataFrame(scaled_data, columns=data.columns)

    return data


def encoding(data):
    if os.path.isfile(POSITION_ENCODER_PATH) and os.path.isfile(FORM_ENCODER_PATH):
        with open(POSITION_ENCODER_PATH, 'rb') as f:
            position_encoder = pickle.load(f)

        with open(FORM_ENCODER_PATH, 'rb') as f:
            form_encoder = pickle.load(f)

        data['physical'] = form_encoder.transform(data['physical'])
        try:
            data['position'] = position_encoder.transform(data['position'])

        except:
            pass

    else:
        form_encoder = LabelEncoder()
        position_encoder = LabelEncoder()

        data['physical'] = form_encoder.fit_transform(data['physical'])
        try:
            data['position'] = position_encoder.fit_transform(data['position'])

        except:
            pass

        with open(POSITION_ENCODER_PATH, 'wb') as f:
            pickle.dump(position_encoder, f, pickle.HIGHEST_PROTOCOL)

        with open(FORM_ENCODER_PATH, 'wb') as f:
            pickle.dump(form_encoder, f, pickle.HIGHEST_PROTOCOL)

    return data, position_encoder, form_encoder


def prepare(df):
    numeric_df = df.select_dtypes(exclude='object')
    object_df = df.select_dtypes(include='object')
    id_df = df['id']

    numeric_df = scale_dataset(numeric_df.drop(['id'], axis=1))
    object_df, position_encoder, form_encoder = encoding(object_df)

    df = pd.concat([id_df, numeric_df, object_df], axis=1)

    return df, position_encoder, form_encoder


def run():
    if os.path.isfile(MODEL_PATH):
        with open(MODEL_PATH, 'rb') as f:
            clf = pickle.load(f)

        return clf

    if not os.path.isfile(DATA_PATH):
        print('전처리된 데이터가 없습니다. 전처리부터 시작합니다.')
        preprocess.run()

    df = pd.read_csv(DATA_PATH)
    df, position_encoder, form_encoder = prepare(df)
    X_train, X_test, y_train, y_test = train_test_split(df.drop(['position'], axis=1),
                                                        df['position'], test_size=0.3, random_state=123)
    X_test_id = X_test[['id', 'name']]
    X_train.drop(['id', 'name'], axis=1, inplace=True)
    X_test.drop(['id', 'name'], axis=1, inplace=True)

    clf = Classifier()
    clf.fit(X_train, y_train)

    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(clf, f, pickle.HIGHEST_PROTOCOL)

    predict = clf.predict(X_test)
    predict = pd.Series(
        map(lambda x: position_encoder.inverse_transform(x), predict))

    print(clf.precision(predict, position_encoder.inverse_transform(y_test)))
    # predict_df = pd.DataFrame()
    # predict_df[['id', 'name']] = X_test_id.reset_index(drop=True)
    # predict_df['position'] = position_encoder.inverse_transform(
    #     y_test.reset_index(drop=True))
    # predict_df['predict_position'] = predict

    return clf
