from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, Form, status, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, SQLModel, create_engine, Session, select,update
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator
from os import environ
from typing import Annotated
from pydantic import HttpUrl,BaseModel, UUID4
from enum import Enum, IntEnum
from aiokafka import AIOKafkaProducer,AIOKafkaConsumer
import asyncio,logging,json


class BaseAccount(SQLModel):
    user_id: int
    name: str
    amount: int 

class Account(BaseAccount, table=True):
    id: int | None = Field(default=None, primary_key=True)

class TransactionType(IntEnum):
    withdraw = 1
    deposit = 2

class Transaction(BaseModel):
    order_id: int | None = None
    user_id: int
    type: TransactionType
    amount: int

logger = logging.getLogger('uvicorn.error')
logger.setLevel(logging.DEBUG)

db_url=environ.get("DB_URL")
kafka_url=environ.get("KAFKA_URL")

engine = create_engine(db_url, pool_pre_ping=True)

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
Instrumentator().instrument(app).expose(app)

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]

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

async def make_transaction(transaction: Transaction):
    producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
    await producer.start()
    logger.debug(transaction)
    with Session(engine) as session:
        user_balance=session.exec(select(Account).where(Account.user_id == transaction.user_id)).first().amount

        if transaction.type==TransactionType.deposit:
            session.exec(update(Account).where(Account.user_id==transaction.user_id).values(amount=Account.amount+transaction.amount))
            session.commit()
            await producer.send_and_wait(
                topic='Billing', 
                key=str(transaction.user_id).encode('utf-8'), 
                value=transaction.model_dump_json().encode('utf-8'), 
                headers=[("type",b"TransactionDeposit"),("status",b"OK")]
                )
            return True
        else:
            if transaction.amount<user_balance:
                session.exec(update(Account).where(Account.user_id==transaction.user_id).values(amount=Account.amount-transaction.amount))
                session.commit()
                await producer.send_and_wait(
                    topic='Billing', 
                    key=str(transaction.user_id).encode('utf-8'), 
                    value=transaction.model_dump_json().encode('utf-8'), 
                    headers=[("type",b"TransactionWithdraw"),("status",b"OK")]
                    )
                return True
            else:
                await producer.send_and_wait(
                    topic='Billing', 
                    key=str(transaction.user_id).encode('utf-8'), 
                    value=transaction.model_dump_json().encode('utf-8'), 
                    headers=[("type",b"TransactionWithdraw"),("status",b"Fail")]
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
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderCreated':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                trans=Transaction(order_id=msg_data['id'],
                                  user_id=msg_data['user_id'],
                                  type=TransactionType.withdraw,
                                  amount=msg_data['amount'])
                await make_transaction(trans)
                
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()