import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

HOST = os.getenv('HOST')
PORT = os.getenv('PORT')
USERID = os.getenv('USERID')
PASSWORD = os.getenv('PASSWORD')


class Client:
    def __init__(self, dbname):
        self.host = HOST
        self.port = PORT
        self.userid = USERID
        self.password = PASSWORD
        self.dbname = dbname
        self.client = MongoClient(
            f'mongodb://{self.userid}:{self.password}@{self.host}', int(self.port))
        self.db = self.client[dbname]
        print('DB와 성공적으로 연결되었습니다.')
