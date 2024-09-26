from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, Form, status, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator
from os import environ
from typing import Annotated
from pydantic import HttpUrl
from aiokafka import AIOKafkaProducer,AIOKafkaConsumer
import asyncio,logging,json

class BaseOrder(SQLModel):
    user_id: int
    name: str
    amount: int 
    status: str = "Created"

class Order(BaseOrder, table=True):
    id: int | None = Field(default=None, primary_key=True)


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

app = FastAPI(lifespan=lifespan, root_path="/api/order")
Instrumentator().instrument(app).expose(app)

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]

@app.get("/health")
async def health():
    return {"status": "OK"}
 
@app.post("/order",response_model=Order, status_code=status.HTTP_201_CREATED)
async def create_order(order: BaseOrder):
    producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
    await producer.start()

    with Session(engine) as session:
        dborder=Order.model_validate(order)
        session.add(dborder)
        session.commit()
        session.refresh(dborder)
        await producer.send_and_wait(topic='Order', key=str(dborder.user_id).encode('utf-8'), value=dborder.model_dump_json().encode('utf-8'), headers=[("type",b"OrderCreated")])
        return dborder

@app.get("/order",response_model=list[Order])
async def get_all_orders(user_info: authDep):
    with Session(engine) as session:
        return session.exec(select(Order)).all()


async def consume():
    consumer = AIOKafkaConsumer('Billing', bootstrap_servers=kafka_url)
    await consumer.start()
    logger.debug("Consumer started")
    try:
        # Consume messages
        async for msg in consumer:
            # logger.debug("consumed: ", msg.topic, msg.partition, msg.offset, msg.key, msg.value, msg.timestamp, msg.headers)
            if msg.topic=="Billing" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'TransactionWithdraw':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                order=Order()
                with Session(engine) as session:
                    order=session.get(Order, int(msg_data['order_id']))
                    order.status="Processed"+dict(msg.headers)['status'].decode()
                    session.add(order)
                    session.commit()
                    session.refresh(order)
                    logger.debug(order)
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(topic='Order', 
                                             key=str(msg_data['user_id']).encode('utf-8'), 
                                             value=order.model_dump_json().encode('utf-8'), 
                                             headers=[("type",b"OrderProcessed"),("status",dict(msg.headers)['status'])])
                await producer.stop()
                
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()