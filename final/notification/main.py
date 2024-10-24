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

class BaseNotification(SQLModel):
    user_id: int
    email: str
    message: str

class Notification(BaseNotification, table=True):
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

app = FastAPI(lifespan=lifespan, root_path="/api/notification")
Instrumentator().instrument(app).expose(app)

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]

@app.get("/health")
async def health():
    return {"status": "OK"}
 
@app.post("/notification",response_model=Notification, status_code=status.HTTP_201_CREATED)
async def create_notification(notif: BaseNotification):
    with Session(engine) as session:
        dbnotif=Notification.model_validate(notif)
        session.add(dbnotif)
        session.commit()
        session.refresh(dbnotif)
        return dbnotif

@app.get("/notification",response_model=list[Notification])
async def get_all_notifications(user_info: authDep):
    with Session(engine) as session:
        return session.exec(select(Notification)).all()

@app.get("/notification/{user_id}",response_model=list[Notification])
async def get_user_notifications(user_id: int, user_info: authDep):
    with Session(engine) as session:
        notif=session.exec(select(Notification).where(Notification.user_id == user_id)).all()
        if not notif:
            raise HTTPException(status_code=404, detail="Notifications for user not found")
        return notif

async def consume():
    consumer = AIOKafkaConsumer('Order', bootstrap_servers=kafka_url)
    await consumer.start()
    logger.debug("Consumer started")
    try:
        # Consume messages
        async for msg in consumer:
            # logger.debug("consumed: ", msg.topic, msg.partition, msg.offset, msg.key, msg.value, msg.timestamp, msg.headers)
            if msg.topic=="Order" and 'type' in dict(msg.headers) and dict(msg.headers)['type']==b'OrderProcessed':
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                logger.debug(dict(msg.headers))
                if dict(msg.headers)['status']==b"OK":
                    msg=f"Order processed succesfully (billing -{msg_data['amount']}, stock -{msg_data['stock_count']})"
                else:
                    msg=f"Order failed (billing -{msg_data['amount']}, stock -{msg_data['stock_count']})" #(-{msg_data['amount']}), not enough money

                notif=Notification(email=f" User's {msg_data['user_id']} email",
                                  user_id=msg_data['user_id'],
                                  message=msg)
                await create_notification(notif)
                
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()