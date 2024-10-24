from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, Form, status, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, SQLModel, create_engine, Session, select,update, func
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator,metrics
from prometheus_client import Counter, Gauge
from os import environ
from typing import Annotated,Callable
from pydantic import HttpUrl,BaseModel, UUID4,computed_field
from enum import Enum, IntEnum
from aiokafka import AIOKafkaProducer,AIOKafkaConsumer
import asyncio,logging,json


class BaseStock(SQLModel):
    title: str
    count: int 

class Stock(BaseStock, table=True):
    id: int | None = Field(default=None, primary_key=True)
    @computed_field(return_type=int)
    @property
    def real_count(self):
        with Session(engine) as session:
            prepare_count=session.exec(
            select(func.sum(Transaction.count))
            .where(Transaction.stock_id == self.id)
            .where(Transaction.type==TransactionType.withdraw)
            .where(Transaction.status == TransactionStatus.prepare).group_by(Transaction.stock_id)
            ).first()
            return (
                self.count-(int(prepare_count) if prepare_count else 0)
            )

class TransactionType(IntEnum):
    withdraw = 1
    deposit = 2

class TransactionStatus(IntEnum):
    prepare = 1
    complete = 2
    rollback = 3
 

class Transaction(SQLModel, table=True): 
    id: int | None = Field(default=None, primary_key=True)
    order_id: int | None = None
    user_id: int
    stock_id: int # = Field(foreign_key=Stock.id)
    type: TransactionType
    count: int
    status: TransactionStatus = TransactionStatus.complete

logger = logging.getLogger('uvicorn.error')
logger.setLevel(logging.DEBUG)

db_url=environ.get("DB_URL")
kafka_url=environ.get("KAFKA_URL")

# engine = create_engine(db_url, pool_pre_ping=True, echo=True)
engine = create_engine("sqlite://", echo=False)


def create_db_and_tables():
    if not database_exists(engine.url):
        create_database(engine.url)
    SQLModel.metadata.create_all(engine)
    # prefill tables
    with Session(engine) as session:
        session.add(Stock(id=1,title="box",count=100))
        session.add(Stock(id=2,title="stool",count=50))
        session.add(Stock(id=3,title="table",count=150))
        session.commit()
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    create_db_and_tables()
    asyncio.create_task(consume())
    yield
    # shutdown

app = FastAPI(lifespan=lifespan, root_path="/api/stock")

def stock_count() -> Callable[[metrics.Info], None]:
    METRIC = Gauge(
        "stock_count", 
        "count of stock items.", 
        labelnames=("stock_id","stock_name")
    )

    def instrumentation(info: metrics.Info) -> None:
        with Session(engine) as session:
            data=session.exec(select(Stock)).all()
            for item in data:
                # print(account)
                METRIC.labels(str(item.id),str(item.title)).set(item.real_count)

    return instrumentation

instr=Instrumentator()
instr.add(stock_count())
instr.add(metrics.default())
instr.instrument(app)
instr.expose(app)

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]

@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/stock",response_model=Stock, status_code=status.HTTP_201_CREATED)
async def create_stock(account: BaseStock):
    with Session(engine) as session:
        dbaccount=Stock.model_validate(account)
        session.add(dbaccount)
        session.commit()
        session.refresh(dbaccount)
        return dbaccount

@app.get("/stock",response_model=Stock | list[Stock])
async def get_stock(user_info: authDep):
    with Session(engine) as session:
        return session.exec(select(Stock)).all()

@app.get("/transaction",response_model=Transaction | list[Transaction])
async def get_transaction(user_info: authDep):
    with Session(engine) as session:
        return session.exec(select(Transaction)).all()
 

# async def get_stock_balance(stock_id):
#     with Session(engine) as session:
#         stock_balance=session.exec(select(Stock).where(Stock.id == stock_id)).first().count
#         prepare_count=session.exec(
#             select(func.sum(Transaction.count))
#             .where(Transaction.stock_id == stock_id)
#             .where(Transaction.type==TransactionType.withdraw)
#             .where(Transaction.status == TransactionStatus.prepare).group_by(Transaction.stock_id)
#             ).first()
#         if not prepare_count:
#             prepare_count=0
#         logger.debug(f"{stock_balance} {prepare_count}")
#         return stock_balance-prepare_count
 
async def make_transaction(transaction: Transaction, rollback=False):
    producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
    await producer.start()
    logger.debug(transaction)
    with Session(engine) as session:
        try:
            stock_balance=session.exec(select(Stock).where(Stock.id == transaction.stock_id)).first().real_count
        except:
            return False
        logger.info(f"{transaction.order_id} - {stock_balance} {transaction.count}")
        if transaction.type==TransactionType.deposit:
            session.exec(update(Stock).where(Stock.id==transaction.stock_id).values(count=Stock.count+transaction.count))
            session.add(transaction)
            session.commit()

            await producer.send_and_wait(
                topic='Stock', 
                key=str(transaction.user_id).encode('utf-8'), 
                value=transaction.model_dump_json().encode('utf-8'), 
                headers=[("type",b"TransactionDeposit"),("status",b"OK")]
                )
            return True
        elif rollback:
            prepared_transaction=session.exec(
                select(Transaction)
                .where(Transaction.user_id == transaction.user_id)
                .where(Transaction.order_id == transaction.order_id)
                .where(Transaction.type==TransactionType.withdraw)
                .where(Transaction.status == TransactionStatus.prepare)
                ).first()
            if (prepared_transaction):
                prepared_transaction.status=TransactionStatus.rollback
                session.add(prepared_transaction)
                session.commit()
            await producer.send_and_wait(
                topic='Stock', 
                key=str(transaction.user_id).encode('utf-8'), 
                value=transaction.model_dump_json().encode('utf-8'), 
                headers=[("type",b"TransactionWithdrawRollback"),("status",b"OK")]
                )
            return True
        elif transaction.status==TransactionStatus.complete:
            # Commit
            prepared_transaction=session.exec(
                select(Transaction)
                .where(Transaction.user_id == transaction.user_id)
                .where(Transaction.order_id == transaction.order_id)
                .where(Transaction.type==TransactionType.withdraw)
                .where(Transaction.status == TransactionStatus.prepare)
                ).first()
            prepared_transaction.status=TransactionStatus.complete
            session.add(prepared_transaction)

            session.exec(update(Stock).where(Stock.id==transaction.stock_id).values(count=Stock.count-transaction.count))
            session.commit()
            await producer.send_and_wait(
                topic='Stock', 
                key=str(transaction.user_id).encode('utf-8'), 
                value=transaction.model_dump_json().encode('utf-8'), 
                headers=[("type",b"TransactionWithdrawCommit"),("status",b"OK")]
                )
            return True
        else:
            # Withdraw good
            if transaction.count<=stock_balance:
                if transaction.status==TransactionStatus.prepare:
                    # Prepare
                    session.add(transaction)
                    session.commit()
                    await producer.send_and_wait(
                        topic='Stock', 
                        key=str(transaction.user_id).encode('utf-8'), 
                        value=transaction.model_dump_json().encode('utf-8'), 
                        headers=[("type",b"TransactionWithdrawPrepare"),("status",b"OK")]
                        )
                return True
            else:
                # Withdraw no stock
                await producer.send_and_wait(
                    topic='Stock', 
                    key=str(transaction.user_id).encode('utf-8'), 
                    value=transaction.model_dump_json().encode('utf-8'), 
                    headers=[("type",b"TransactionWithdrawPrepare"),("status",b"Fail")]
                    )
                return False


@app.post("/transaction",response_model=Stock, status_code=status.HTTP_200_OK)
async def create_transaction(transaction: Transaction):
    with Session(engine) as session:
        trans = await make_transaction(transaction)
        stock=session.exec(select(Stock).where(Stock.id == transaction.stock_id)).first()
        if trans:
            return stock
        else:
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=f"Not enough stock of '{stock.title}', {stock.count}<{transaction.count}")


async def consume():
    consumer = AIOKafkaConsumer('Order', 'User', bootstrap_servers=kafka_url)
    await consumer.start()
    logger.debug("Consumer started")
    try:
        # Consume messages
        async for msg in consumer:
            logger.debug(f"consumed: , {msg.topic}, {msg.partition}, {msg.offset}, {msg.key}, {msg.value}, {msg.timestamp}, {msg.headers}")
            if msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderPrepare':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  stock_id=msg_data['stock_id'],
                                  type=TransactionType.withdraw,
                                  count=msg_data['stock_count'],
                                  status=TransactionStatus.prepare)
                await make_transaction(trans)
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderCommit':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  stock_id=msg_data['stock_id'],
                                  type=TransactionType.withdraw,
                                  count=msg_data['stock_count'],
                                  status=TransactionStatus.complete)
                await make_transaction(trans)
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderRollback':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  stock_id=msg_data['stock_id'],
                                  type=TransactionType.withdraw,
                                  count=msg_data['stock_count'],
                                  status=TransactionStatus.complete)
                await make_transaction(trans,rollback=True)
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()