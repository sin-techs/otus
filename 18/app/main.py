from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator
from os import environ


class BaseUser(SQLModel):
    login: str = Field(title="User login", max_length=50)
    firstname: str
    lastname: str
    email: str
    age: int | None = None

class User(BaseUser, table=True):
    id: int | None = Field(default=None, primary_key=True)

class UserUpdate(SQLModel):
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    age: int | None = None


db_url=environ.get("DB_URL")

engine = create_engine(db_url)

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


@app.get("/health")
async def root():
    return {"status": "OK"}

@app.post("/users",response_model=User)
async def create_user(user: BaseUser):
    with Session(engine) as session:
        dbuser=User.model_validate(user)
        session.add(dbuser)
        session.commit()
        session.refresh(dbuser)
        return dbuser

@app.get("/users",response_model=list[User])
async def get_all_users():
    with Session(engine) as session:
        return session.exec(select(User)).all()

@app.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int):
    with Session(engine) as session:
        user=session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

@app.delete("/users/{user_id}")
async def delete_user(user_id: int):
    with Session(engine) as session:
        user=session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        session.delete(user)
        session.commit()
        return {"ok":True, "message":f"User {user_id} deleted"}

@app.put("/users/{user_id}", response_model=User)
async def update_user(user_id: int, user: UserUpdate):
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

  
