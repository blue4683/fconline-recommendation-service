import os
import pickle

import numpy as np
import pandas as pd
from sklearn.preprocessing import LabelEncoder, StandardScaler
from xgboost.sklearn import XGBClassifier

from FRS.core.position_recommend.data_contract import InputContractError
from FRS.core.position_recommend.evaluation import position_set
from FRS.schema.models import Position

DATA_PATH = "FRS/utils/data/position_recommend_data.csv"
FORM_ENCODER_PATH = "FRS/utils/data/form_encoder.pickle"
POSITION_ENCODER_PATH = "FRS/utils/data/position_encoder.pickle"
SCALER_PATH = "FRS/utils/data/position_recommend_scaler.pickle"


def scale_dataset(data):
    if os.path.isfile(SCALER_PATH):
        with open(SCALER_PATH, "rb") as f:
            scaler = pickle.load(f)

        scaled_data = scaler.transform(data)

    else:
        scaler = StandardScaler()
        scaled_data = scaler.fit_transform(data)
        with open(SCALER_PATH, "wb") as f:
            pickle.dump(scaler, f, pickle.HIGHEST_PROTOCOL)

    data = pd.DataFrame(scaled_data, columns=data.columns)

    return data


def encoding(data):
    if os.path.isfile(POSITION_ENCODER_PATH) and os.path.isfile(FORM_ENCODER_PATH):
        with open(POSITION_ENCODER_PATH, "rb") as f:
            position_encoder = pickle.load(f)

        with open(FORM_ENCODER_PATH, "rb") as f:
            form_encoder = pickle.load(f)

        data["physical"] = form_encoder.transform(data["physical"])
        try:
            data["position"] = position_encoder.transform(data["position"])

        except Exception:
            pass

    else:
        form_encoder = LabelEncoder()
        position_encoder = LabelEncoder()

        data["physical"] = form_encoder.fit_transform(data["physical"])
        try:
            data["position"] = position_encoder.fit_transform(data["position"])

        except Exception:
            pass

        with open(POSITION_ENCODER_PATH, "wb") as f:
            pickle.dump(position_encoder, f, pickle.HIGHEST_PROTOCOL)

        with open(FORM_ENCODER_PATH, "wb") as f:
            pickle.dump(form_encoder, f, pickle.HIGHEST_PROTOCOL)

    return data, position_encoder, form_encoder


def prepare(df):
    numeric_df = df.select_dtypes(exclude="object")
    object_df = df.select_dtypes(include="object")
    id_df = df["id"]

    numeric_df = scale_dataset(numeric_df.drop(["id"], axis=1))
    object_df, position_encoder, form_encoder = encoding(object_df)

    df = pd.concat([id_df, numeric_df, object_df], axis=1)

    return df, position_encoder, form_encoder


class Classifier:
    def __init__(self):
        self.model = XGBClassifier(
            objective="multi:softprob",
            n_estimators=50,
            max_depth=5,
            gamma=0,
            importance_type="gain",
            reg_lambda=1,
            random_state=100,
        )

    def call(self, MODEL_PATH):
        if not os.path.isfile(MODEL_PATH):
            raise FileNotFoundError(
                "레거시 모델이 없습니다. 명시한 새 모델 번들을 사용하세요."
            )

        with open(MODEL_PATH, "rb") as f:
            self.model = pickle.load(f)

    def train(self, MODEL_PATH):
        raise InputContractError(
            "레거시의 행 분할/전체 fit 학습은 중단했습니다. "
            "python -m FRS.core.position_recommend.train의 명시 실행을 사용하세요."
        )

    def fit(self, X, y):
        self.model.fit(X, y)

    def predict(self, X):
        predict = self.model.predict_proba(X)
        predict = list(map(lambda x: np.where(x >= 0.1)[0], predict))

        return predict

    def precision(self, predict, y):
        # 단일 코드와 등록 코드 목록을 통일하고 기존 반환 단위(%)를 유지한다.
        predictions, labels = list(predict), list(y)
        if not labels or len(predictions) != len(labels):
            raise InputContractError("예측과 정답의 행 수가 다르거나 비었습니다.")
        hits = 0
        for predicted, label in zip(predictions, labels, strict=True):
            if isinstance(predicted, str):
                raise InputContractError("예측 포지션은 코드 목록이어야 합니다.")
            codes = {Position(p).value for p in predicted}
            actual = position_set((label,) if isinstance(label, str) else label)
            hits += bool(codes & actual)
        return hits / len(labels) * 100
