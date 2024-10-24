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
from typing import Annotated, Optional,Callable
from pydantic import HttpUrl,BaseModel, UUID4, computed_field 
from enum import Enum, IntEnum
from aiokafka import AIOKafkaProducer,AIOKafkaConsumer
import asyncio,logging,json


class BaseAccount(SQLModel):
    user_id: int
    name: str
    amount: int 
    @computed_field(return_type=int)
    @property
    def real_amount(self):
        with Session(engine) as session:
            prepare_amount=session.exec(
            select(func.sum(Transaction.amount))
            .where(Transaction.user_id == self.user_id)
            .where(Transaction.type==TransactionType.withdraw)
            .where(Transaction.status == TransactionStatus.prepare).group_by(Transaction.user_id)
            ).first()
            return (
                self.amount-(int(prepare_amount) if prepare_amount else 0)
            )

class Account(BaseAccount, table=True):
    id: int | None = Field(default=None, primary_key=True)

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
    type: TransactionType
    amount: int
    status: TransactionStatus = TransactionStatus.complete

logger = logging.getLogger('uvicorn.error')
logger.setLevel(logging.DEBUG)

db_url=environ.get("DB_URL")
kafka_url=environ.get("KAFKA_URL")

engine = create_engine(db_url, pool_pre_ping=True, echo=True)

def create_db_and_tables():
    if not database_exists(engine.url):
        create_database(engine.url)

    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    create_db_and_tables()
    asyncio.create_task(consume())
    yield
    # shutdown

app = FastAPI(lifespan=lifespan, root_path="/api/billing")

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]


def account_balance() -> Callable[[metrics.Info], None]:
    METRIC = Gauge(
        "account_balance", 
        "Balance of user acount.", 
        labelnames=("user_id","account_id")
    )

    def instrumentation(info: metrics.Info) -> None:
        with Session(engine) as session:
            accounts=session.exec(select(Account)).all()
            for account in accounts:
                print(account)
                METRIC.labels(str(account.user_id),str(account.id)).set(account.real_amount)
 
    return instrumentation

instr=Instrumentator()
instr.add(account_balance())
instr.add(metrics.default())
instr.instrument(app).expose(app)



@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/account",response_model=Account, status_code=status.HTTP_201_CREATED)
async def create_account(account: BaseAccount):
    with Session(engine) as session:
        dbaccount=Account.model_validate(account)
        session.add(dbaccount)
        session.commit()
        session.refresh(dbaccount)
        return dbaccount

@app.get("/account",response_model=Account | list[Account])
async def get_accounts(user_info: authDep,user_id:int=0):
    with Session(engine) as session:
        if user_id==0:
            return session.exec(select(Account)).all()

        account=session.exec(select(Account).where(Account.user_id == user_id)).first()
        if account:
            return account
        else:
            raise HTTPException(status_code=404, detail="Account not found")

@app.get("/transaction",response_model=Transaction | list[Transaction])
async def get_accounts(user_info: authDep,user_id:int=0):
    with Session(engine) as session:
        if user_id==0:
            return session.exec(select(Transaction)).all()

        account=session.exec(select(Transaction).where(Transaction.user_id == user_id)).all()
        if account:
            return account
        else:
            raise HTTPException(status_code=404, detail="Account not found")

# async def get_account_balance(user_id):
#     with Session(engine) as session:
#         account_balance=session.exec(select(Account).where(Account.user_id == user_id)).first().amount
#         prepare_amount=session.exec(
#             select(func.sum(Transaction.amount))
#             .where(Transaction.user_id == user_id)
#             .where(Transaction.type==TransactionType.withdraw)
#             .where(Transaction.status == TransactionStatus.prepare).group_by(Transaction.user_id)
#             ).first()
#         if not prepare_amount:
#             prepare_amount=0
#         logger.debug(f"{account_balance} {prepare_amount}")
#         return account_balance-prepare_amount

async def make_transaction(transaction: Transaction, rollback=False):
    producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
    await producer.start()
    logger.debug(transaction)
    with Session(engine) as session:
        user_balance=session.exec(select(Account).where(Account.user_id == transaction.user_id)).first().real_amount

        if transaction.type==TransactionType.deposit:
            session.exec(update(Account).where(Account.user_id==transaction.user_id).values(amount=Account.amount+transaction.amount))
            session.add(transaction)
            session.commit()

            await producer.send_and_wait(
                topic='Billing', 
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
            if prepared_transaction:
                prepared_transaction.status=TransactionStatus.rollback
                session.add(prepared_transaction)
                session.commit()
            await producer.send_and_wait(
                topic='Billing', 
                key=str(transaction.user_id).encode('utf-8'), 
                value=transaction.model_dump_json().encode('utf-8'), 
                headers=[("type",b"TransactionWithdrawRollback"),("status",b"OK")]
                )
        elif transaction.status==TransactionStatus.complete:
            # Commit
            prepared_transaction=session.exec(
                select(Transaction)
                .where(Transaction.user_id == transaction.user_id)
                .where(Transaction.order_id == transaction.order_id)
                .where(Transaction.type==TransactionType.withdraw)
                .where(Transaction.status == TransactionStatus.prepare)
                ).one()
            prepared_transaction.status=TransactionStatus.complete
            session.add(prepared_transaction)

            session.exec(update(Account).where(Account.user_id==transaction.user_id).values(amount=Account.amount-transaction.amount))
            session.commit()
            await producer.send_and_wait(
                topic='Billing', 
                key=str(transaction.user_id).encode('utf-8'), 
                value=transaction.model_dump_json().encode('utf-8'), 
                headers=[("type",b"TransactionWithdrawCommit"),("status",b"OK")]
                )
            return True
        else:
            # Withdraw good
            if transaction.amount<=user_balance:
                if transaction.status==TransactionStatus.prepare:
                    # Prepare
                    session.add(transaction)
                    session.commit()
                    await producer.send_and_wait(
                        topic='Billing', 
                        key=str(transaction.user_id).encode('utf-8'), 
                        value=transaction.model_dump_json().encode('utf-8'), 
                        headers=[("type",b"TransactionWithdrawPrepare"),("status",b"OK")]
                        )
                    return True
            else:
                # Withdraw no money
                await producer.send_and_wait(
                    topic='Billing', 
                    key=str(transaction.user_id).encode('utf-8'), 
                    value=transaction.model_dump_json().encode('utf-8'), 
                    headers=[("type",b"TransactionWithdrawPrepare"),("status",b"Fail")]
                    )
                return False


@app.post("/transaction",response_model=Account, status_code=status.HTTP_200_OK)
async def create_transaction(transaction: Transaction):
    with Session(engine) as session:
        trans = await make_transaction(transaction)
        account=session.exec(select(Account).where(Account.user_id == transaction.user_id)).first()
        if trans:
            return account
        else:
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=f"Not enough money, {account.amount}<{transaction.amount}")


async def consume():
    consumer = AIOKafkaConsumer('Order', 'User', bootstrap_servers=kafka_url)
    await consumer.start()
    logger.debug("Consumer started")
    try:
        # Consume messages
        async for msg in consumer:
            # logger.debug("consumed: ", msg.topic, msg.partition, msg.offset, msg.key, msg.value, msg.timestamp, msg.headers)
            if msg.topic=="User" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'UserCreated':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                account=BaseAccount(name=f"User {msg_data['id']} account",
                                  user_id=msg_data['id'],
                                  amount=0)
                await create_account(account)
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderPrepare':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  type=TransactionType.withdraw,
                                  amount=msg_data['amount'],
                                  status=TransactionStatus.prepare)
                await make_transaction(trans)
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderCommit':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  type=TransactionType.withdraw,
                                  amount=msg_data['amount'],
                                  status=TransactionStatus.complete)
                await make_transaction(trans)
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderRollback':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  type=TransactionType.withdraw,
                                  amount=msg_data['amount'],
                                  status=TransactionStatus.complete)
                await make_transaction(trans,rollback=True)
                
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()