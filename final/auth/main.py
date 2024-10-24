from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request,Response, Form, status
from fastapi.responses import RedirectResponse, HTMLResponse,JSONResponse
# from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Annotated
from pydantic import HttpUrl
from sqlmodel import Field, SQLModel, create_engine, Session, select
from sqlalchemy_utils import create_database, database_exists
from prometheus_fastapi_instrumentator import Instrumentator
# from os import environ
import jwt
import time
from typing import Dict
from decouple import config
import json
import urllib.parse

class BaseUser(SQLModel):
    login: str = Field(title="User login", max_length=50)
    password: str 

class User(BaseUser, table=True):
    id: int | None = Field(default=None, primary_key=True)

JWT_SECRET = config("JWT_SECRET")
JWT_ALGORITHM = config("JWT_ALGORITHM")

engine = create_engine(config("DB_URL"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    yield
    # shutdown

app = FastAPI(lifespan=lifespan)
Instrumentator().instrument(app).expose(app)

templates = Jinja2Templates(directory="templates")

@app.get("/")
async def root():
    return "Hello World!"

@app.get("/health")
async def health():
    return {"status": "OK"}


@app.get("/validate")
async def validate(req: Request, resp: Response):
    print("headers",req.headers) 
    print("cookies",req.cookies) 
    path = urllib.parse.urlparse(req.headers['x-original-url']).path
    method=req.headers['x-original-method']

    print(path,method)
    # skip open paths
    if (path=="/api/users" and method=="POST"):
        return "OK"
 
    # return "Hello World!"
    # return RedirectResponse("/login")
    if (authdata:=decode_jwt(req.cookies.get('x-auth'))) or (authdata:=decode_jwt(req.headers.get('x-auth'))):
        print(authdata)
        resp.headers['x-auth-user']=str(authdata['user_id'])
        return "OK"
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

@app.post("/token")
async def get_token(auth: BaseUser):
    with Session(engine) as session:
        res=session.exec(select(User).where(User.login==auth.login,User.password==auth.password))
        user=res.first()
        if user:
            # print(user)
            user_id=user.id
        else:
           raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    response=JSONResponse(content={"token":sign_jwt(user_id)})
    return response


@app.get("/login", response_class=HTMLResponse)
async def get_login(req: Request,rd:str = ''):
    return templates.TemplateResponse(
        request=req, name="login.html", context={"redir":rd,"msg":{}}
    )

@app.post("/login")
async def post_login(req: Request, login: Annotated[str, Form()], password: Annotated[str, Form()], redir: Annotated[str, Form()] = ''):
    try:
        token= await get_token(BaseUser(login=login,password=password))
    except HTTPException:
        return templates.TemplateResponse(request=req, name="login.html", context={"redir":redir,"msg":{"color":"red","text":"Login Failed!"}})

    token_data=json.loads(token.body)
    try:
        HttpUrl(url=redir)
        response =RedirectResponse(redir, status_code=status.HTTP_303_SEE_OTHER)
    except:
        response =RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="x-auth", value=token_data['token'])
    return response

@app.get("/logout")
async def logout():
    response =RedirectResponse('/', status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="x-auth")
    return response


def sign_jwt(user_id: str) -> Dict[str, str]:
    payload = {
        "user_id": user_id,
        "expires": time.time() + 60*60
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    return token

def decode_jwt(token: str) -> dict:
    try:
        decoded_token = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return decoded_token if decoded_token["expires"] >= time.time() else None
    except:
        return {}