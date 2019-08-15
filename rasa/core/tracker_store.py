import json
import logging
import pickle
import typing
from typing import Iterator, Optional, Text, Iterable, Union

import itertools

# noinspection PyPep8Naming
from time import sleep

from rasa.core.actions.action import ACTION_LISTEN_NAME
from rasa.core.broker import EventChannel
from rasa.core.domain import Domain
from rasa.core.trackers import ActionExecuted, DialogueStateTracker, EventVerbosity
from rasa.utils.common import class_from_module_path

if typing.TYPE_CHECKING:
    from sqlalchemy.engine.url import URL
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


class TrackerStore(object):
    # 用来维护所有tracker的，相当于tracker管理器
    #
    # 这是个基类，具体trackerStore的实现可以由很多方式
    # 内存中（InMemoryTrackerStore）
    # redis中（RedisTrackerStore）
    # mongo中（MongoTrackerStore）
    # sql数据库中（SQLTrackerStore）（rasa默认使用的是这个，具体用sqlite,也可以通过配置改为MySQL）
    # 通过继承TrackerStore，可以实现自定义的tracker store

    def __init__(
        self, domain: Optional[Domain], event_broker: Optional[EventChannel] = None
    ) -> None:
        self.domain = domain
        self.event_broker = event_broker  # TODO:搞清楚event_broker作用
        self.max_event_history = None  # TODO:搞清楚max_event_history作用

    @staticmethod
    def find_tracker_store(domain, store=None, event_broker=None):
        if store is None or store.type is None:
            # 没有指定store 或者没有指定store类型时，默认用InMemoryTrackerStore
            tracker_store = InMemoryTrackerStore(domain, event_broker=event_broker)
        elif store.type == "redis":
            tracker_store = RedisTrackerStore(
                domain=domain, host=store.url, event_broker=event_broker, **store.kwargs
            )
        elif store.type == "mongod":
            tracker_store = MongoTrackerStore(
                domain=domain, host=store.url, event_broker=event_broker, **store.kwargs
            )
        elif store.type.lower() == "sql":
            tracker_store = SQLTrackerStore(
                domain=domain, host=store.url, event_broker=event_broker, **store.kwargs
            )
        else:
            # 指定store的类型不是redis  mongod  sql中的任意一种
            # store.type 是自定义的tracker类名
            tracker_store = TrackerStore.load_tracker_from_module_string(domain, store)

        logger.debug("Connected to {}.".format(tracker_store.__class__.__name__))
        # logger.debug("Connected to {}.".format(rasa.core.utils.module_path_from_instance(tracker_store)))
        return tracker_store

    @staticmethod
    def load_tracker_from_module_string(domain, store):
        custom_tracker = None
        try:
            custom_tracker = class_from_module_path(store.type) # 依据类名找类对象
        except (AttributeError, ImportError):
            logger.warning(
                "Store type '{}' not found. "
                "Using InMemoryTrackerStore instead".format(store.type)
            )

        if custom_tracker:
            return custom_tracker(domain=domain, url=store.url, **store.kwargs)
        else:
            return InMemoryTrackerStore(domain)  # event_broker=event_broker ?

    def get_or_create_tracker(self, sender_id, max_event_history=None):
        tracker = self.retrieve(sender_id)
        self.max_event_history = max_event_history  # max_event_history设置为store全局的
        if tracker is None:
            tracker = self.create_tracker(sender_id)
        return tracker

    def init_tracker(self, sender_id):
        # 新建一个tracker对象
        return DialogueStateTracker(
            sender_id,
            self.domain.slots if self.domain else None,  # 所有的tracker公用store的domain，也就是说rasa 对所有用户的功能都一样，不存在“和A用户谈法律，和B用户聊医疗”的情况
            max_event_history=self.max_event_history,
        )

    def create_tracker(self, sender_id, append_action_listen=True):
        """Creates a new tracker for the sender_id.

        The tracker is initially listening."""

        tracker = self.init_tracker(sender_id) #
        if tracker:  # 直接写成 if tracker and append_action_listen:  不好吗
            if append_action_listen:
                tracker.update(ActionExecuted(ACTION_LISTEN_NAME))  # 为新建的tracker添加listen
            self.save(tracker)  # 把新建的tracker保存到tracker_store中管理起来
        return tracker

    def save(self, tracker):
        # 虚函数，把tracker放到tracker store中管理
        raise NotImplementedError()

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        # 查找对应的tracker
        # 虚函数，留给子类实现
        raise NotImplementedError()

    def stream_events(self, tracker: DialogueStateTracker) -> None:
        offset = self.number_of_existing_events(tracker.sender_id)  # 这个ID记录在自己tracker中的事件数量
        evts = tracker.events
        for evt in list(itertools.islice(evts, offset, len(evts))):  # offset == len(evts) ?
            body = {"sender_id": tracker.sender_id}
            body.update(evt.as_dict())
            self.event_broker.publish(body)  # TODO: 作用？

    def number_of_existing_events(self, sender_id: Text) -> int:
        """Return number of stored events for a given sender id."""
        old_tracker = self.retrieve(sender_id)
        return len(old_tracker.events) if old_tracker else 0

    def keys(self) -> Iterable[Text]:
        # 类字典使用方式
        raise NotImplementedError()

    @staticmethod
    def serialise_tracker(tracker):
        # serialise：连载；使连续；序列化
        # tracker --> dialogue str
        dialogue = tracker.as_dialogue()
        return pickle.dumps(dialogue)

    def deserialise_tracker(self, sender_id, _json) -> Optional[DialogueStateTracker]:
        # deserialise: 反序列化
        # dialogue str --> tracker
        dialogue = pickle.loads(_json)
        tracker = self.init_tracker(sender_id)
        if tracker:
            tracker.recreate_from_dialogue(dialogue)
            return tracker
        else:
            return None


class InMemoryTrackerStore(TrackerStore):

    def __init__(
        self, domain: Domain, event_broker: Optional[EventChannel] = None
    ) -> None:
        self.store = {}  # 新增属性，各个tracker在内存中以字典形式组织，key为sender_id，value为序列化的tracker
        super(InMemoryTrackerStore, self).__init__(domain, event_broker)

    def save(self, tracker: DialogueStateTracker) -> None:
        # 把tracker添加到store中
        if self.event_broker:
            self.stream_events(tracker) # todo:作用？
        serialised = InMemoryTrackerStore.serialise_tracker(tracker)
        self.store[tracker.sender_id] = serialised

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        # 查找特定的tracker
        if sender_id in self.store:
            logger.debug("Recreating tracker for id '{}'".format(sender_id))
            return self.deserialise_tracker(sender_id, self.store[sender_id])
        else:
            logger.debug("Creating a new tracker for id '{}'.".format(sender_id))
            return None

    def keys(self) -> Iterable[Text]:
        # 返回所有受维护的sender_id
        return self.store.keys()


class RedisTrackerStore(TrackerStore):
    def keys(self) -> Iterable[Text]:
        return self.red.keys()

    def __init__(
        self,
        domain,
        host="localhost",
        port=6379,
        db=0,
        password=None,
        event_broker=None,
        record_exp=None,
    ):

        import redis

        self.red = redis.StrictRedis(host=host, port=port, db=db, password=password)  # 连接redis
        self.record_exp = record_exp  # redis过期时间（秒）
        super(RedisTrackerStore, self).__init__(domain, event_broker)

    def save(self, tracker, timeout=None):
        if self.event_broker:
            self.stream_events(tracker)

        if not timeout and self.record_exp:  # 方法中和store中都设置了过期时间，按store的过期时间算
            timeout = self.record_exp

        serialised_tracker = self.serialise_tracker(tracker)
        self.red.set(tracker.sender_id, serialised_tracker, ex=timeout)

    def retrieve(self, sender_id):
        stored = self.red.get(sender_id)
        if stored is not None:
            return self.deserialise_tracker(sender_id, stored)
        else:
            return None
# 使用redis来存储tracker的方法：
#     1.启动redis服务
#
#     2.配置 endpoints.yml
#
#         tracker_store:
#             type: redis
#             url: <url of the redis instance, e.g. localhost>
#             port: <port of your redis instance, usually 6379>
#             db: <number of your database within redis, e.g. 0>
#             password: <password used for authentication>
#
#     3. To start the Rasa Core server using your configured Redis instance, add the --endpoints flag, e.g.:
#
#         rasa run -m models --endpoints endpoints.yml
#
# #################################################

class MongoTrackerStore(TrackerStore):
    def __init__(
        self,
        domain,
        host="mongodb://localhost:27017",
        db="rasa",
        username=None,
        password=None,
        auth_source="admin",
        collection="conversations",
        event_broker=None,
    ):
        from pymongo.database import Database
        from pymongo import MongoClient

        self.client = MongoClient(
            host,
            username=username,
            password=password,
            authSource=auth_source,
            # delay connect until process forking is done
            connect=False,
        )

        self.db = Database(self.client, db)
        self.collection = collection
        super(MongoTrackerStore, self).__init__(domain, event_broker)

        self._ensure_indices()

    @property
    def conversations(self):
        return self.db[self.collection]

    def _ensure_indices(self):
        self.conversations.create_index("sender_id")

    def save(self, tracker, timeout=None):
        if self.event_broker:
            self.stream_events(tracker)

        state = tracker.current_state(EventVerbosity.ALL)

        self.conversations.update_one(
            {"sender_id": tracker.sender_id}, {"$set": state}, upsert=True
        )

    def retrieve(self, sender_id):
        stored = self.conversations.find_one({"sender_id": sender_id})

        # look for conversations which have used an `int` sender_id in the past
        # and update them.
        if stored is None and sender_id.isdigit():
            from pymongo import ReturnDocument

            stored = self.conversations.find_one_and_update(
                {"sender_id": int(sender_id)},
                {"$set": {"sender_id": str(sender_id)}},
                return_document=ReturnDocument.AFTER,
            )

        if stored is not None:
            if self.domain:
                return DialogueStateTracker.from_dict(
                    sender_id, stored.get("events"), self.domain.slots
                )
            else:
                logger.warning(
                    "Can't recreate tracker from mongo storage "
                    "because no domain is set. Returning `None` "
                    "instead."
                )
                return None
        else:
            return None

    def keys(self) -> Iterable[Text]:
        return [c["sender_id"] for c in self.conversations.find()]


# 使用mongoDB来存储tracker的方法
#     1.Start your MongoDB instance.
#
#     2.Add required configuration to your endpoints.yml
#
#         tracker_store:
#             type: mongod
#             url: <url to your mongo instance, e.g. mongodb://localhost:27017>
#             db: <name of the db within your mongo instance, e.g. rasa>
#             username: <username used for authentication>
#             password: <password used for authentication>
#             auth_source: <database name associated with the user’s credentials>
#
#         You can also add more advanced configurations (like enabling ssl)
#         by appending a parameter to the url field, e.g. mongodb://localhost:27017/?ssl=true
#
#     3. To start the Rasa Core server using your configured MongoDB instance, add the --endpoints flag, e.g.:
#
#             rasa run -m models --endpoints endpoints.yml
#
# ###################################################################

class SQLTrackerStore(TrackerStore):
    """Store which can save and retrieve trackers from an SQL database."""
    # 在表中记录的是事件
    # 创建会话时(retrieve)，导出该sender_id的所有事件， 然后用这些事件重放成一个内存中的tracker对象
    # 会话过程中，正常使用tracker进行记录
    # 会话结束后(save)，把tracker对象中新产生的事件逐条写入数据库
    from sqlalchemy.ext.declarative import declarative_base

    Base = declarative_base()

    class SQLEvent(Base):
        from sqlalchemy import Column, Integer, String, Float, Text

        __tablename__ = "events"

        id = Column(Integer, primary_key=True)
        sender_id = Column(String(255), nullable=False, index=True)
        type_name = Column(String(255), nullable=False)
        timestamp = Column(Float)
        intent_name = Column(String(255))
        action_name = Column(String(255))
        data = Column(Text)

    # 连接数据库，建库建表
    def __init__(
        self,
        domain: Optional[Domain] = None,
        dialect: Text = "sqlite",  # 默认使用sqlite数据库
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "rasa.db",  # sqlite数据库 的名称为rasa.db
        username: Text = None,
        password: Text = None,
        event_broker: Optional[EventChannel] = None,
        login_db: Optional[Text] = None,
    ) -> None:
        import sqlalchemy
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy import create_engine

        engine_url = self.get_db_url(
            dialect, host, port, db, username, password, login_db
        )
        logger.debug(
            "Attempting to connect to database " 'via "{}"'.format(repr(engine_url))
        )

        # Database might take a while to come up
        while True:
            try:
                self.engine = create_engine(engine_url)

                # if `login_db` has been provided, use current connection with
                # that database to create working database `db`
                if login_db:
                    self._create_database_and_update_engine(db, engine_url)

                try:
                    self.Base.metadata.create_all(self.engine)
                except (
                    sqlalchemy.exc.OperationalError,
                    sqlalchemy.exc.ProgrammingError,
                ) as e:
                    # Several Rasa services started in parallel may attempt to
                    # create tables at the same time. That is okay so long as
                    # the first services finishes the table creation.
                    logger.error("Could not create tables: {}".format(e))

                self.session = sessionmaker(bind=self.engine)()
                break
            except (
                sqlalchemy.exc.OperationalError,
                sqlalchemy.exc.IntegrityError,
            ) as e:

                logger.warning(e)
                sleep(5)

        logger.debug("Connection to SQL database '{}' successful".format(db))

        super(SQLTrackerStore, self).__init__(domain, event_broker)

    @staticmethod
    def get_db_url(
        dialect: Text = "sqlite",
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "rasa.db",
        username: Text = None,
        password: Text = None,
        login_db: Optional[Text] = None,
    ) -> Union[Text, "URL"]:
        """Builds an SQLAlchemy `URL` object representing the parameters needed
        to connect to an SQL database.

        Args:
            dialect: SQL database type.
            host: Database network host.
            port: Database network port.
            db: Database name.
            username: User name to use when connecting to the database.
            password: Password for database user.
            login_db: Alternative database name to which initially connect, and create
                the database specified by `db` (PostgreSQL only).

        Returns:
            URL ready to be used with an SQLAlchemy `Engine` object.

        """
        from urllib.parse import urlsplit
        from sqlalchemy.engine.url import URL

        # Users might specify a url in the host
        parsed = urlsplit(host or "")
        if parsed.scheme:
            return host

        if host:
            # add fake scheme to properly parse components
            parsed = urlsplit("schema://" + host)

            # users might include the port in the url
            port = parsed.port or port
            host = parsed.hostname or host

        return URL(
            dialect,
            username,
            password,
            host,
            port,
            database=login_db if login_db else db,
        )

    def _create_database_and_update_engine(self, db: Text, engine_url: "URL"):
        """Create databse `db` and update engine to reflect the updated
            `engine_url`."""

        from sqlalchemy import create_engine

        self._create_database(self.engine, db)
        engine_url.database = db
        self.engine = create_engine(engine_url)

    @staticmethod
    def _create_database(engine: "Engine", db: Text):
        """Create database `db` on `engine` if it does not exist."""

        import psycopg2

        conn = engine.connect()

        cursor = conn.connection.cursor()
        cursor.execute("COMMIT")
        cursor.execute(
            ("SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{}'".format(db))
        )
        exists = cursor.fetchone()
        if not exists:
            try:
                cursor.execute("CREATE DATABASE {}".format(db))
            except psycopg2.IntegrityError as e:
                logger.error("Could not create database '{}': {}".format(db, e))

        cursor.close()
        conn.close()

    def keys(self) -> Iterable[Text]:
        sender_ids = self.session.query(self.SQLEvent.sender_id).distinct().all()  # 无重复的所有senderID
        return [sender_id for (sender_id,) in sender_ids]

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        """Create a tracker from all previously stored events."""
        # 基于数据库中记录的events，重放出一个tracker
        query = self.session.query(self.SQLEvent)
        result = query.filter_by(sender_id=sender_id).all()
        events = [json.loads(event.data) for event in result]

        if self.domain and len(events) > 0:
            # store定义了domain，并且存在至少一条event
            logger.debug("Recreating tracker from sender id '{}'".format(sender_id))

            return DialogueStateTracker.from_dict(sender_id, events, self.domain.slots)  # 基于事件创建tracker
        else:
            # 要么store没定义domain， 要么对应sender id 没有事件
            logger.debug(
                "Can't retrieve tracker matching"
                "sender id '{}' from SQL storage.  "
                "Returning `None` instead.".format(sender_id)
            )
            return None

    def save(self, tracker: DialogueStateTracker) -> None:
        """Update database with events from the current conversation."""

        if self.event_broker:  # 好几处都出现了这段代码，搞成装饰器？
            self.stream_events(tracker)

        events = self._additional_events(tracker)  # only store recent events
        # 写在tracker中，但还不在数据库中的新events
        for event in events:
            # 从单个事件中抽取信息
            data = event.as_dict()

            intent = data.get("parse_data", {}).get("intent", {}).get("name")
            action = data.get("name")
            timestamp = data.get("timestamp")

            # noinspection PyArgumentList
            self.session.add(
                self.SQLEvent(
                    sender_id=tracker.sender_id,
                    type_name=event.type_name,
                    timestamp=timestamp,
                    intent_name=intent,
                    action_name=action,
                    data=json.dumps(data),
                )
            )
        # 逐条保存新发生的事件到数据库中
        self.session.commit()

        logger.debug(
            "Tracker with sender_id '{}' "
            "stored to database".format(tracker.sender_id)
        )

    def number_of_existing_events(self, sender_id: Text) -> int:
        """Return number of stored events for a given sender id."""

        query = self.session.query(self.SQLEvent.sender_id)
        # 返回数据库中sender id记录在案的event条数
        return query.filter_by(sender_id=sender_id).count() or 0

    def _additional_events(self, tracker: DialogueStateTracker) -> Iterator:
        """Return events from the tracker which aren't currently stored."""
        n_events = self.number_of_existing_events(tracker.sender_id)
        # 返回在tracker中新加的(没来得及写入数据库的)events
        return itertools.islice(tracker.events, n_events, len(tracker.events))
