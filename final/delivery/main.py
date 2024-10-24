from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, Form, status, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, SQLModel, create_engine, Session, select,update, func
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator
from os import environ
from typing import Annotated
from pydantic import HttpUrl,BaseModel, UUID4
from enum import Enum, IntEnum
from aiokafka import AIOKafkaProducer,AIOKafkaConsumer
import asyncio,logging,json


logger = logging.getLogger('uvicorn.error')
logger.setLevel(logging.DEBUG)

db_url=environ.get("DB_URL")
kafka_url=environ.get("KAFKA_URL")

# engine = create_engine(db_url, pool_pre_ping=True, echo=True)
engine = create_engine("sqlite://", echo=True)


def create_db_and_tables():
    if not database_exists(engine.url):
        create_database(engine.url)
    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    # create_db_and_tables()
    asyncio.create_task(consume())
    yield
    # shutdown

app = FastAPI(lifespan=lifespan, root_path="/api/stock")
Instrumentator().instrument(app).expose(app)

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]

@app.get("/health")
async def health():
    return {"status": "OK"}

async def consume():
    consumer = AIOKafkaConsumer('Order', bootstrap_servers=kafka_url)
    await consumer.start()
    logger.debug("Consumer started")
    try:
        # Consume messages
        async for msg in consumer:
            logger.debug(f"{msg.topic} - {msg.headers}, {msg.value}")
            # logger.debug("consumed: ", msg.topic, msg.partition, msg.offset, msg.key, msg.value, msg.timestamp, msg.headers)
            if msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderPrepare':
                msg_data=json.loads((msg.value).decode())
                msg_data['order_id']=msg_data['id']
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(
                        topic='Delivery', 
                        key=msg.key, 
                        value=json.dumps(msg_data).encode(), 
                        headers=[("type",b"TransactionWithdrawPrepare"),("status",b"OK")]
                        )
                await producer.stop()
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderCommit':
                msg_data=json.loads((msg.value).decode())
                msg_data['order_id']=msg_data['id']
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(
                        topic='Delivery', 
                        key=msg.key, 
                        value=json.dumps(msg_data).encode(), 
                        headers=[("type",b"TransactionWithdrawCommit"),("status",b"OK")]
                        )
                await producer.stop()
            elif msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderRollback':
                msg_data=json.loads((msg.value).decode())
                msg_data['order_id']=msg_data['id']
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(
                        topic='Delivery', 
                        key=msg.key, 
                        value=json.dumps(msg_data).encode(), 
                        headers=[("type",b"TransactionWithdrawRollback"),("status",b"OK")]
                        )
                await producer.stop()
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()