from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, Form, status, Depends, Header
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, SQLModel, create_engine, Session, select, update
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator
from os import environ
from typing import Annotated
from pydantic import HttpUrl
from aiokafka import AIOKafkaProducer,AIOKafkaConsumer
import asyncio,logging,json,uuid
from functools import wraps

class BaseOrder(SQLModel):
    user_id: int
    name: str
    amount: int 
    stock_id: int
    stock_count: int
    status: str = "Created"
    status_billing: str = "pending"
    status_stock: str = "pending"
    status_delivery: str = "pending"

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
    asyncio.create_task(consume('Billing'))
    asyncio.create_task(consume('Stock'))
    asyncio.create_task(consume('Delivery'))
    yield
    # shutdown

app = FastAPI(lifespan=lifespan, root_path="/api/order")
Instrumentator().instrument(app).expose(app)

app.mount("/static", StaticFiles(directory="static"), name="static") 
templates = Jinja2Templates(directory="templates")

async def get_auth_user(req: Request):
    user_id=req.headers.get("x-auth-user")
    return {"user_id":user_id}

authDep = Annotated[dict, Depends(get_auth_user)]

idempotency_keys = dict()

async def create_order_2pc(order: BaseOrder):
    pass

async def check_order(order_id):
    order_status=dict()
    with Session(engine) as session:
        # order=session.exec(select(Order).where(Order.id==order_id)).first()
        order=session.get(Order, order_id)
        order_status['billing']=order.status_billing
        order_status['stock']=order.status_stock
        order_status['delivery']=order.status_delivery
        
        # check for failed status
        for service,status in order_status.items():
            if status=="failed":
                logger.debug(f"{service} - {status}")
                if service=="billing":
                    order.status_billing='pending'
                elif service=="stock":
                    order.status_stock='pending'
                elif service=="delivery":
                    order.status_delivery='pending'
                order.status="failed: "+service
                session.add(order)
                session.commit()
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(topic='Order', key=str(order.user_id).encode('utf-8'), value=order.model_dump_json().encode('utf-8'), headers=[("type",b"OrderRollback")])
                await producer.stop()     
                return
        # check for common status
        uniq_status=set(order_status.values())
        if len(uniq_status)==1: 
            if "prepared" in uniq_status:
                logger.debug("All services - Prepared!")
                order.status="prepared"
                session.add(order)
                session.commit()
                # Prepared succesfully in all services
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(topic='Order', key=str(order.user_id).encode('utf-8'), value=order.model_dump_json().encode('utf-8'), headers=[("type",b"OrderCommit")])
                await producer.stop()     
            elif "commited" in uniq_status:
                logger.debug("All services - Commited! Order Finished")
                # session.exec(update(Order).where(Order.id==order_id).values(status="finished"))
                order.status="finished"
                session.add(order)
                session.commit()
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(topic='Order', 
                                             key=str(order.user_id).encode('utf-8'), 
                                             value=order.model_dump_json().encode('utf-8'), 
                                             headers=[("type",b"OrderProcessed"),("status",b"OK")])
                await producer.stop()
            elif "rolledback" in uniq_status:
                logger.debug("All services - Rolledback! Order Failed")
                # session.exec(update(Order).where(Order.id==order_id).values(status="finished"))
                # order.status=order.status+" - rolledback"
                session.add(order)
                session.commit()
                producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
                await producer.start()
                await producer.send_and_wait(topic='Order', 
                                             key=str(order.user_id).encode(), 
                                             value=order.model_dump_json().encode(), 
                                             headers=[("type",b"OrderProcessed"),("status",b"Fail")])
                await producer.stop()


@app.get("/health")
async def health():
    return {"status": "OK"}

@app.get("/order-key")
async def order_key():
    key=str(uuid.uuid4())
    idempotency_keys.update({key:0})
    return {"key": key}
 

@app.post("/order",response_model=Order, status_code=status.HTTP_201_CREATED)
async def create_order(order: BaseOrder, idempotency_key: Annotated[str | None, Header()] = None):
    print(idempotency_keys,idempotency_key)
    if idempotency_key not in idempotency_keys:
        raise HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE, detail="Key not found")
    if idempotency_keys[idempotency_key]==0:
        idempotency_keys[idempotency_key]+=1
    else:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Already processing")

    producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
    await producer.start()

    with Session(engine) as session:
        dborder=Order.model_validate(order)
        session.add(dborder)
        session.commit()
        session.refresh(dborder)
        await producer.send_and_wait(topic='Order', key=str(dborder.user_id).encode('utf-8'), value=dborder.model_dump_json().encode('utf-8'), headers=[("type",b"OrderPrepare")])
        return dborder

@app.get("/order",response_model=list[Order])
@app.get("/order/{order_id}",response_model=Order)
async def get_all_orders(user_info: authDep,user_id:int=0,order_id:int=None):
    with Session(engine) as session:
        if order_id:
            order=session.get(Order, order_id)
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")
            return order
        if user_id:
            orders=session.exec(select(Order).where(Order.user_id == user_id)).all()
            if not orders:
                raise HTTPException(status_code=404, detail="Orders not found")
            return orders
        else:
            return session.exec(select(Order)).all()
            

async def consume(topic):
    consumer = AIOKafkaConsumer(topic, bootstrap_servers=kafka_url)
    await consumer.start()
    logger.debug(f"Consumer {topic} started")

    order_statuses={b"OK":"prepared",b"Fail":"failed"}

    try: 
        # Consume messages
        async for msg in consumer:
            logger.debug(f"consumed: {msg.topic}, {msg.partition}, {msg.offset}, {msg.key}, {msg.value}, {msg.timestamp}, {msg.headers}")

            if (
                msg.topic in ["Billing","Stock","Delivery"] and 
                'type' in dict(msg.headers) and dict(msg.headers)['type'] in [b'TransactionWithdrawPrepare',b'TransactionWithdrawCommit',b'TransactionWithdrawRollback']
                ):
                logger.debug(msg.value)
                msg_data=json.loads((msg.value).decode())
                with Session(engine) as session:
                    order=session.get(Order, int(msg_data['order_id']))
                    # order.status=msg.topic+": "+dict(msg.headers)['type'].decode()+" "+dict(msg.headers)['status'].decode()
                     
                    order_status=order_statuses[dict(msg.headers)['status']]
                    if dict(msg.headers)['type']==b'TransactionWithdrawCommit' and order_status=="prepared":
                        order_status="commited"
                    elif dict(msg.headers)['type']==b'TransactionWithdrawRollback' and order_status=="prepared":
                        order_status="rolledback"

                    if msg.topic=="Billing":
                        order.status_billing=order_status
                    elif msg.topic=="Stock":
                        order.status_stock=order_status
                    elif msg.topic=="Delivery":
                        order.status_delivery=order_status

                    session.add(order)
                    session.commit()
                    await check_order(order_id=order.id)
                 
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()
