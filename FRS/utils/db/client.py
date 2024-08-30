from pymongo import MongoClient


class Client:
    def __init__(self, host, port, userid, password, dbname):
        self.host = host
        self.port = port
        self.userid = userid
        self.password = password
        self.dbname = dbname
        self.client = MongoClient(
            f'mongodb://{self.userid}:{self.password}@{self.host}', int(self.port))
        self.db = self.client[dbname]
        print('DB와 성공적으로 연결되었습니다.')
