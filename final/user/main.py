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

class BaseUser(SQLModel):
    login: str = Field(title="User login", max_length=50, unique=True)
    password: str 
    firstname: str 
    lastname: str 
    email: str 
    age: int = 0

class User(BaseUser, table=True):
    id: int | None = Field(default=None, primary_key=True)

class UserUpdate(SQLModel):
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    age: int | None = None


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
    yield
    # shutdown

app = FastAPI(lifespan=lifespan)
Instrumentator().instrument(app).expose(app)

app.mount("/static", StaticFiles(directory="static"), name="static") 
templates = Jinja2Templates(directory="templates")

async def get_auth_user(req: Request):
    print(req.headers)
    user_id=req.headers.get("x-auth-user")
    return {"user_id":(user_id if user_id else 0)}

authDep = Annotated[dict, Depends(get_auth_user)]

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(req: Request):
    auth=None
    if req.cookies.get("x-auth") or req.headers.get("x-auth"):
        auth=True
    print(auth)
    return templates.TemplateResponse(
        request=req, name="index.html", context={"auth":auth}
    )

@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    file_name = "favicon.ico"
    return FileResponse(path='/app/static/favicon.ico', headers={"Content-Disposition": "attachment; filename=" + file_name})


@app.get("/health")
async def health():
    return {"status": "OK"}

@app.post("/api/users",response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(user: BaseUser):
    with Session(engine) as session:
        dbuser=User.model_validate(user)
        session.add(dbuser)
        session.commit()
        session.refresh(dbuser)
        producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
        await producer.start()
        await producer.send_and_wait(topic='User', 
                                        key=str(dbuser.id).encode('utf-8'), 
                                        value=dbuser.model_dump_json().encode('utf-8'), 
                                        headers=[("type",b"UserCreated")])
        await producer.stop()
        return dbuser

@app.get("/api/users",response_model=list[User])
async def get_all_users(user_info: authDep):
    with Session(engine) as session:
        return session.exec(select(User)).all()

@app.get("/api/users/{user_id}", response_model=User)
async def get_user(user_id: int,user_info: authDep):
    if user_id!=int(user_info['user_id']):
            raise HTTPException(status_code=403, detail="Wrong user, not allowed")
    with Session(engine) as session:
        user=session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int,user_info: authDep):
    # if user_id!=int(user_info['user_id']):
    #         raise HTTPException(status_code=403, detail="Wrong user, not allowed")
    with Session(engine) as session:
        user=session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        session.delete(user)
        session.commit()
        return {"ok":True, "message":f"User {user_id} deleted"}

@app.put("/api/users/{user_id}", response_model=User)
async def update_user(user_id: int, user: UserUpdate,user_info: authDep):
    # if user_id!=int(user_info['user_id']):
    #         raise HTTPException(status_code=403, detail="Wrong user, not allowed")
    with Session(engine) as session:
        dbuser=session.get(User, user_id)
        if not dbuser:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")
        user_data=user.model_dump(exclude_unset=True)
        dbuser.sqlmodel_update(user_data)
        session.add(dbuser)
        session.commit()
        session.refresh(dbuser)
        return dbuser

  
@app.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def get_register(req: Request):
    user_id=req.headers.get("x-auth-user")

    return templates.TemplateResponse(
        request=req, name="register.html", context={}
    )
 
@app.post("/register", response_class=HTMLResponse, include_in_schema=False)
async def post_register(req: Request, 
                        login: Annotated[str, Form()], 
                        password: Annotated[str, Form()], 
                        firstname: Annotated[str, Form()], 
                        lastname: Annotated[str, Form()], 
                        email: Annotated[str, Form()]):
    user=BaseUser(login=login,password=password,firstname=firstname,lastname=lastname,email=email)
    try:
        user_id= await create_user(user)
        msg=f"Created successfully: {login}"
        color="green"
    except:
        msg=f"Error creating {login}"
        color="red"

    return templates.TemplateResponse(
        request=req, name="register.html", context={"msg":msg, "color":color}
    )

@app.get("/profile", response_class=HTMLResponse, include_in_schema=False)
async def profile(req: Request, user_info: authDep):
    user= await get_user(int(user_info['user_id']),user_info=user_info)
    return templates.TemplateResponse(
        request=req, name="profile.html", context={"auth":True, "user":dict(user)}
    )

@app.post("/profile", response_class=HTMLResponse, include_in_schema=False)
async def post_profile(req: Request, user_info: authDep,
                       firstname: Annotated[str, Form()], 
                       lastname: Annotated[str, Form()], 
                       age: Annotated[str, Form()], 
                       email: Annotated[str, Form()]):
    
    new_user=UserUpdate(firstname=firstname,
                        lastname=lastname,
                        email=email,
                        age=age)
    user= await update_user(int(user_info['user_id']),user=new_user,user_info=user_info)
    return templates.TemplateResponse(
        request=req, name="profile.html", context={"auth":True,"user":dict(user)}
    )   