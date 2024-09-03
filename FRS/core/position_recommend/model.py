from xgboost.sklearn import XGBClassifier

import numpy as np
import pandas as pd


class Classifier:
    def __init__(self, model=None):
        self.model = XGBClassifier(
            objective='multi:softprob',
            n_estimators=50,
            max_depth=5,
            gamma=0,
            importance_type='gain',
            reg_lambda=1,
            random_state=100
        ) if model == None else model

    def fit(self, X, y):
        self.model.fit(X, y)

    def predict(self, X):
        predict = self.model.predict_proba(X)
        predict = list(map(lambda x: np.where(x >= 0.1)[0], predict))

        return predict

    def precision(self, predict, y):
        total = len(y)
        result = 0
        for pred, position in zip(predict, y):
            if position in pred:
                result += 1

        return (result / total) * 100
